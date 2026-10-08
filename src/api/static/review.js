// Review queue: keyboard-driven, optimistic, prefetching.
import { api, chip, confidenceColor, displayName, el, emit, modal, on, toast, url } from "./api.js";

const POLL_MS = 5000;

const q = {
  buffer: [],
  pos: 0,
  seen: new Set(),
  skipped: new Set(),
  done: 0,
  total: 0,
  filters: {},
  loading: false,
  exhausted: false,
  inFlight: 0,
  since: null, // server time of the last look for new items
  newCount: 0, // items added at the end since the queue was loaded
  doneAtCount: 0, // q.done when q.total was last read from the server
  othersSkipped: [], // names of people whose items were skipped
  pollTimer: null,
  countsTimer: null,
};

const key = (item) => `${item.scan_id}/${item.name}`;

export function initReview() {
  document.getElementById("rv-load").addEventListener("click", () => load());
  document.getElementById("rv-template").addEventListener("change", refreshNames);
  document.getElementById("rv-wide").addEventListener("change", () => show());
  document.getElementById("rv-grid").addEventListener("change", () => show());
  document.getElementById("rv-order").addEventListener("change", () => load());
  // The queue loads when the tab opens (no "Load queue" press needed)
  document.querySelector('.tabs button[data-tab="review"]').addEventListener("click", () => {
    if (q.since === null && !q.loading) setTimeout(() => load(), 0);
  });
  document.getElementById("rv-accept-bulk").addEventListener("click", acceptBulk);
  on("templates", refreshNames);
  on("review-changed", refreshBadge);
  on("review-scan", (scanId) => {
    document.getElementById("rv-scan").value = scanId;
    document.getElementById("rv-template").value = "";
    document.getElementById("rv-name").value = "";
    document.querySelector('.tabs button[data-tab="review"]').click();
    load();
  });
  on("review-job", (jobId) => {
    document.getElementById("rv-job").value = jobId;
    document.getElementById("rv-scan").value = "";
    document.querySelector('.tabs button[data-tab="review"]').click();
    load();
  });
  refreshBadge();
  startPolling();
}

export async function refreshBadge() {
  try {
    const summary = await api("/review/summary");
    const badge = document.getElementById("review-badge");
    badge.textContent = summary.total > 9999 ? "9999+" : summary.total;
    badge.classList.toggle("hidden", !summary.total);
  } catch (e) {
    /* ignore */
  }
}

async function refreshNames() {
  const select = document.getElementById("rv-name");
  const template = document.getElementById("rv-template").value;
  const current = select.value;
  try {
    const summary = await api(`/review/summary${template ? `?template_id=${encodeURIComponent(template)}` : ""}`);
    fillNames(summary.by_name, summary.total, current);
  } catch (e) {
    /* ignore */
  }
}

function fillNames(rows, total, current) {
  const select = document.getElementById("rv-name");
  if (current === undefined) current = select.value;
  select.innerHTML = "";
  select.append(el("option", { value: "" }, `All (${total})`));
  const names = new Set();
  for (const row of rows) {
    if (names.has(row.name)) continue;
    names.add(row.name);
    const n = rows.filter((r) => r.name === row.name).reduce((sum, r) => sum + r.n, 0);
    select.append(el("option", { value: row.name }, `${displayName(row.name, "(no name)")} (${n})`));
  }
  // Keep the chosen name even when nothing is left for it
  if (current && !names.has(current)) select.append(el("option", { value: current }, `${current} (0)`));
  select.value = current || "";
}

function filterParams(extra) {
  const params = new URLSearchParams(extra || {});
  for (const [k, v] of Object.entries(q.filters)) if (v) params.set(k, v);
  return params;
}

// Pending count and per-name counts from the server, after a save or skip
function refreshCounts(delay = 400) {
  clearTimeout(q.countsTimer);
  q.countsTimer = setTimeout(async () => {
    if (q.inFlight) return refreshCounts(300);
    try {
      const data = await api(`/review/counts?${filterParams()}`);
      q.total = data.total;
      q.doneAtCount = q.done;
      if (document.getElementById("rv-template").value === (q.filters.template_id || "")) fillNames(data.by_name, data.all_names);
      updateProgress();
    } catch (e) {
      /* ignore */
    }
  }, delay);
}

function reviewTabOpen() {
  const tab = document.getElementById("tab-review");
  return tab && tab.classList.contains("active") && !document.hidden;
}

function startPolling() {
  clearInterval(q.pollTimer);
  q.pollTimer = setInterval(() => {
    if (!reviewTabOpen() || q.since === null) return;
    pollNew();
    checkStates();
  }, POLL_MS);
}

// New items of a running job (or from another screen) go to the end of the list
async function pollNew() {
  try {
    const data = await api(`/review?${filterParams({ limit: "200", created_after: String(q.since) })}`);
    let added = 0;
    for (const item of data.items) {
      if (q.seen.has(key(item))) continue;
      q.seen.add(key(item));
      q.buffer.push(item);
      added++;
    }
    q.since = data.now;
    if (!added) return;
    const wasFinished = !current();
    q.newCount += added;
    q.exhausted = false;
    refreshCounts(0);
    if (wasFinished) show();
    else updateProgress();
  } catch (e) {
    /* offline for a moment: try again on the next tick */
  }
}

// Items someone else already decided are marked "done by X" and skipped
async function checkStates() {
  const ahead = q.buffer.slice(q.pos, q.pos + 40).filter((item) => item.decided === undefined && !item.doneBy && !item.gone);
  if (!ahead.length) return;
  let data;
  try {
    data = await api("/review/states", { method: "POST", json: { items: ahead.map((i) => ({ scan_id: i.scan_id, name: i.name })) } });
  } catch (e) {
    return;
  }
  const byKey = Object.fromEntries(data.items.map((s) => [`${s.scan_id}/${s.name}`, s]));
  let currentChanged = false;
  for (const item of ahead) {
    const st = byKey[key(item)];
    if (!st || st.state === "pending") continue;
    if (item.decided !== undefined) continue; // saved here meanwhile
    if (st.state === "done") {
      item.doneBy = st.by || "someone";
      item.doneValue = st.value;
    } else item.gone = true;
    if (item === current()) currentChanged = true;
  }
  if (currentChanged) {
    const input = document.querySelector("#rv-main .rv-value");
    const untouched = !input || input.value === (current().value || "");
    if (untouched && document.activeElement === input) {
      if (current().doneBy) q.othersSkipped.push(current().doneBy);
      move(1);
    }
    else show();
  }
}

function othersDone(item) {
  return item && item.decided === undefined && (item.doneBy || item.gone);
}

function readFilters() {
  const value = (id) => document.getElementById(id).value.trim();
  return {
    template_id: value("rv-template"),
    name: value("rv-name"),
    kind: value("rv-kind"),
    job_id: value("rv-job"),
    scan_id: value("rv-scan"),
    order: value("rv-order"),
  };
}

async function load() {
  Object.assign(q, { buffer: [], pos: 0, seen: new Set(), skipped: new Set(), done: 0, exhausted: false, since: null, newCount: 0, doneAtCount: 0, othersSkipped: [] });
  q.filters = readFilters();
  await fetchMore();
  show();
  checkStates();
}

async function fetchMore() {
  if (q.loading || q.exhausted) return;
  q.loading = true;
  try {
    const params = new URLSearchParams({ limit: "100", offset: String(q.skipped.size) });
    for (const [k, v] of Object.entries(q.filters)) if (v) params.set(k, v);
    const data = await api(`/review?${params}`);
    q.total = data.total;
    q.doneAtCount = q.done;
    if (q.since === null && data.now !== undefined) q.since = data.now;
    let added = 0;
    for (const item of data.items) {
      if (q.seen.has(key(item))) continue;
      q.seen.add(key(item));
      q.buffer.push(item);
      added++;
    }
    if (!added && q.inFlight === 0) q.exhausted = true;
  } catch (error) {
    toast(error.message, "error");
  } finally {
    q.loading = false;
  }
}

function current() {
  return q.buffer[q.pos];
}

function cropUrl(item, pad) {
  return item.crop_url ? url(`${item.crop_url}&pad=${pad}&outline=true`) : null;
}

function pad() {
  return document.getElementById("rv-wide").checked ? 140 : 24;
}

function updateProgress() {
  const remaining = Math.max(q.total - (q.done - q.doneAtCount), 0);
  const parts = [`Reviewed ${q.done} this session`, `${remaining} pending`, `${q.skipped.size} skipped`];
  if (q.othersSkipped.length) {
    const names = [...new Set(q.othersSkipped)];
    parts.push(`${q.othersSkipped.length} done by ${names.slice(0, 3).join(", ")}${names.length > 3 ? "…" : ""}`);
  }
  const progress = document.getElementById("rv-progress");
  progress.textContent = parts.join(" · ");
  if (q.newCount) progress.append(" ", el("span", { class: "chip ok", title: "Added to the end of the list while you were reviewing" }, `${q.newCount} new item${q.newCount === 1 ? "" : "s"}`));
}

function show() {
  const main = document.getElementById("rv-main");
  main.innerHTML = "";
  updateProgress();
  const item = current();
  if (!item) {
    if (!q.exhausted && !q.loading && q.buffer.length) {
      main.append(el("div", { class: "empty-state" }, "Loading…"));
      setTimeout(() => fetchMore().then(show), 300);
      return;
    }
    main.append(el("div", { class: "rv-done" }, q.buffer.length ? "Queue finished. Nice work." : "Nothing to review for these filters."));
    return;
  }
  // Prefetch upcoming crops so moving on is instant
  for (const next of q.buffer.slice(q.pos + 1, q.pos + 6)) {
    const src = cropUrl(next, pad());
    if (src) new Image().src = src;
  }
  if (q.buffer.length - q.pos < 15) fetchMore();
  if (document.getElementById("rv-grid").checked) return showGrid(main, item);

  const value = item.decided !== undefined ? item.decided : item.value;
  const input = el("input", { class: "rv-value", value, spellcheck: "false", autocomplete: "off" });
  const optionsBox = el("div", { class: "rv-options" });
  const renderOptions = () => {
    optionsBox.innerHTML = "";
    (item.options || []).forEach((option, index) => {
      const on = (splitValue(input.value, item.options.map((o) => o.value)) || []).includes(option.value);
      optionsBox.append(
        el(
          "button",
          {
            class: on ? "on" : "",
            title: `fill ${Math.round((option.fill_ratio || 0) * 100)}% · Alt+${index + 1} (Alt+Shift for multi-mark)`,
            onclick: (e) => {
              pick(input, item, index, e.shiftKey);
              renderOptions();
              input.focus();
            },
          },
          el("span", { class: "v" }, option.value),
          el("span", { class: "fill" }, el("div", { style: { width: `${Math.round((option.fill_ratio || 0) * 100)}%` } })),
          el("span", { class: "k" }, index < 9 ? `Alt+${index + 1}` : "")
        )
      );
    });
  };
  renderOptions();
  input.addEventListener("input", () => {
    input.classList.remove("invalid");
    renderOptions();
  });
  input.addEventListener("keydown", (e) => onKey(e, input, item, renderOptions));

  const src = cropUrl(item, pad());
  main.append(
    el(
      "div",
      { class: "rv-card" },
      el("div", { class: "rv-crop" }, src ? cropImage(src, item.name) : el("span", { class: "muted" }, "No image stored for this sheet")),
      el(
        "div",
        { class: "rv-side" },
        el("div", { class: "row gap" }, el("h3", {}, item.name), el("span", { class: "chip" }, item.type)),
        el("div", { class: "rv-meta" }, item.file_id, " · ", el("a", { href: url(`/scans/${item.scan_id}`), target: "_blank" }, item.scan_id.slice(0, 8))),
        el(
          "div",
          { class: "row gap wrap" },
          el("span", { class: "conf", style: { background: confidenceColor(item.confidence) } }, `conf ${item.confidence === null || item.confidence === undefined ? "–" : Number(item.confidence).toFixed(2)}`),
          (item.flags || []).map((f) => chip(f, "flag"))
        ),
        el("div", { class: "rv-meta" }, "Read as: ", el("code", {}, item.value === "" ? "(blank)" : item.value), item.decided !== undefined ? el("span", {}, " · you saved: ", el("code", {}, item.decided === "" ? "(blank)" : item.decided)) : null),
        othersDone(item)
          ? el(
              "div",
              { class: "res-warning" },
              item.gone ? "This item is no longer pending (the sheet was deleted, regraded or settled elsewhere)." : `Done by ${item.doneBy}${item.doneValue !== undefined && item.doneValue !== null ? ` (value ${item.doneValue === "" ? "blank" : item.doneValue})` : ""}. Saving here changes their decision.`,
              " ",
              el("button", { class: "small", onclick: () => move(1) }, "Next")
            )
          : null,
        (item.reasons || []).length ? el("div", { class: "rv-meta rv-reasons" }, item.reasons.join("; ")) : null,
        item.fields ? el("div", { class: "rv-meta" }, "Columns: ", groupColumns(item), " (space = blank column)") : null,
        input,
        item.options ? optionsBox : null,
        (item.candidates || []).length
          ? el(
              "div",
              { class: "rv-options" },
              item.candidates.map((c) =>
                el(
                  "button",
                  {
                    title: `use the value read from ${c.source}`,
                    onclick: () => {
                      input.value = c.value;
                      input.focus();
                    },
                  },
                  el("span", { class: "v" }, c.value),
                  el("span", { class: "k" }, c.source)
                )
              )
            )
          : null,
        el(
          "div",
          { class: "row gap" },
          el("button", { class: "primary", onclick: () => submit(input, item) }, "Save (Enter)"),
          el("button", { onclick: () => move(1, true) }, "Skip (Tab)"),
          el("button", { class: "ghost", onclick: () => move(-1) }, "Back")
        ),
        item.details && Object.keys(item.details).length ? el("details", { class: "rv-meta" }, el("summary", {}, "Reader details"), el("pre", {}, JSON.stringify(item.details, null, 1))) : null
      )
    )
  );
  input.focus();
  input.select();
}

function onKey(e, input, item, renderOptions) {
  if (e.key === "Enter") {
    e.preventDefault();
    submit(input, item);
  } else if (e.key === "Tab") {
    e.preventDefault();
    move(e.shiftKey ? -1 : 1, !e.shiftKey);
  } else if (e.key === "Escape") {
    e.preventDefault();
    input.value = item.decided !== undefined ? item.decided : item.value;
    input.select();
    renderOptions();
  } else if (e.altKey && /^[1-9]$/.test(e.key) && item.options) {
    e.preventDefault();
    pick(input, item, Number(e.key) - 1, e.shiftKey);
    renderOptions();
  } else if (e.altKey && /^Digit[1-9]$/.test(e.code) && item.options) {
    // Alt+Shift+digit produces a symbol on most layouts; use the physical key
    e.preventDefault();
    pick(input, item, Number(e.code.slice(5)) - 1, e.shiftKey);
    renderOptions();
  }
}

function pick(input, item, index, multi) {
  const values = item.options.map((o) => o.value);
  if (index >= values.length) return;
  const chosen = values[index];
  if (!multi) {
    input.value = input.value === chosen ? "" : chosen;
    return;
  }
  const marks = new Set(splitValue(input.value, values) || []);
  if (marks.has(chosen)) marks.delete(chosen);
  else marks.add(chosen);
  input.value = values.filter((v) => marks.has(v)).join("");
}

export function splitValue(value, values) {
  if (!value) return [];
  if (values.includes(value)) return [value];
  const ordered = [...new Set(values)].sort((a, b) => b.length - a.length);
  const out = [];
  let rest = value;
  while (rest) {
    const match = ordered.find((v) => v && rest.startsWith(v));
    if (!match) return null;
    out.push(match);
    rest = rest.slice(match.length);
  }
  return out;
}

function normalize(value, item) {
  value = value.trim();
  if (item.options) {
    const values = item.options.map((o) => o.value);
    if (splitValue(value, values) === null && splitValue(value.toUpperCase(), values) !== null) value = value.toUpperCase();
    // Keep multi-marks in the sheet's option order
    const parts = splitValue(value, values);
    if (parts && parts.length > 1) value = values.filter((v) => parts.includes(v)).join("");
  }
  return value;
}

function submit(input, item) {
  const value = normalize(input.value, item);
  if (item.options && splitValue(value, item.options.map((o) => o.value)) === null) {
    input.classList.remove("invalid");
    void input.offsetWidth;
    input.classList.add("invalid");
    toast(`'${value}' is not made of the options ${item.options.map((o) => o.value).join(", ")}`, "error", 2000);
    return;
  }
  const body = value === item.value ? { accept: [item.name] } : { corrections: { [item.name]: value } };
  sendDecision(item, value, body);
  move(1);
}

function sendDecision(item, value, body) {
  const firstTime = item.decided === undefined;
  item.decided = value;
  if (firstTime) q.done++;
  q.inFlight++;
  api(`/scans/${item.scan_id}/review`, { method: "POST", json: body })
    .then(() => emit("review-saved", { scan_id: item.scan_id, name: item.name }))
    .catch((error) => {
      toast(`${item.name} on ${item.file_id}: ${error.message}`, "error", 6000);
      if (firstTime) q.done--;
      delete item.decided;
    })
    .finally(() => {
      q.inFlight--;
      if (q.inFlight === 0) refreshBadge();
      updateProgress();
      refreshCounts();
    });
}

// Batch by field: the same field from many sheets as a grid of crops; the
// ticked ones are accepted as read together, the others open one by one
function showGrid(main, item) {
  const same = [];
  for (let i = q.pos; i < q.buffer.length && same.length < 30; i++) {
    const it = q.buffer[i];
    if (it.name === item.name && it.decided === undefined && !othersDone(it)) same.push([i, it]);
  }
  const ticks = new Map();
  const tiles = same.map(([index, it]) => {
    const tick = el("input", { type: "checkbox", checked: true });
    ticks.set(it, tick);
    const src = cropUrl(it, 24);
    return el(
      "div",
      { class: "rv-tile" },
      src ? el("img", { src, alt: it.name, onclick: () => (tick.checked = !tick.checked) }) : el("div", { class: "muted small" }, "no image"),
      el(
        "div",
        { class: "row gap" },
        tick,
        el("code", {}, it.value === "" || it.value === null || it.value === undefined ? "∅" : String(it.value)),
        el("span", { class: "spacer" }),
        el("button", { class: "small ghost", title: "Review this one on its own", onclick: () => { document.getElementById("rv-grid").checked = false; q.pos = index; show(); } }, "Open")
      ),
      el("div", { class: "muted small rv-tile-file" }, it.file_id || it.scan_id)
    );
  });
  const acceptTicked = () => {
    const chosen = same.map(([, it]) => it).filter((it) => ticks.get(it).checked);
    if (!chosen.length) return toast("Tick the crops that read right");
    for (const it of chosen) sendDecision(it, it.value, { accept: [it.name] });
    const left = same.filter(([, it]) => !ticks.get(it).checked);
    if (left.length) {
      // The unticked ones are corrected one by one
      document.getElementById("rv-grid").checked = false;
      q.pos = left[0][0];
      toast(`${left.length} left to correct one by one`);
    } else {
      while (q.pos < q.buffer.length && current() && current().decided !== undefined) q.pos++;
    }
    show();
  };
  main.append(
    el(
      "div",
      { class: "row gap" },
      el("h3", {}, `${displayName(item.name)}: ${same.length} sheet(s)`),
      el("span", { class: "muted small" }, "Untick any crop that read wrong, then accept the rest. Unticked crops stay in the queue."),
      el("span", { class: "spacer" }),
      el("button", { class: "primary", onclick: acceptTicked }, "Accept ticked")
    ),
    el("div", { class: "rv-grid" }, tiles)
  );
}

function move(delta, skip = false) {
  const item = current();
  if (skip && item && item.decided === undefined) {
    q.skipped.add(key(item));
    refreshCounts();
  }
  q.pos = Math.max(0, Math.min(q.pos + delta, q.buffer.length));
  // Going forward, step over items someone else decided meanwhile
  while (delta > 0 && othersDone(current())) {
    const done = current();
    if (done.doneBy) q.othersSkipped.push(done.doneBy);
    q.pos++;
  }
  show();
  checkStates();
}

// Columns of a grouped value, coloured by what each column read as
function groupColumns(item) {
  const states = {};
  for (const column of (item.group && item.group.columns) || []) if (column && column.name) states[column.name] = column.state;
  const flagged = new Set(Object.keys(item.field_flags || {}));
  return item.fields.map((name, index) => {
    const state = states[name] || (flagged.has(name) ? "issue" : "");
    const bad = state === "multi" || state === "issue" || flagged.has(name);
    return el(
      "span",
      {},
      index ? ", " : "",
      el("code", { class: bad ? "rv-col-bad" : state === "empty" ? "rv-col-empty" : "", title: state ? `${name}: ${state}` : name }, name)
    );
  });
}

// "Accept as read" for every pending item under the filters: recorded with
// who and when, nothing is deleted
let bulkRun = null;

// Accept in chunks without blocking the page; progress shows in a corner box
async function runBulkAccept(filters, counts) {
  const run = { stop: false };
  bulkRun = run;
  const text = el("span", {}, `Accepting 0 of ${counts.total}…`);
  const stop = el("button", { class: "small", onclick: () => { run.stop = true; stop.disabled = true; text.textContent += " stopping"; } }, "Stop");
  const box = el("div", { class: "bulk-progress", role: "status" }, text, stop);
  document.body.append(box);
  let accepted = 0;
  try {
    while (!run.stop) {
      const body = { ...Object.fromEntries(Object.entries(filters).filter(([, v]) => v)), expected: counts.total, before: counts.now, limit: 500 };
      const done = await api("/review/accept-bulk", { method: "POST", json: body });
      accepted += done.accepted;
      text.textContent = `Accepting ${accepted} of ${counts.total}…`;
      refreshBadge();
      if (done.errors && done.errors.length) toast(`${done.errors.length} sheet(s) could not be updated: ${done.errors[0].error}`, "error", 8000);
      if (!done.remaining || !done.accepted) break;
    }
    toast(`Accepted ${accepted} item(s) as read${run.stop ? " (stopped)" : ""}`, "ok");
  } catch (error) {
    toast(`Accept all stopped after ${accepted} item(s): ${error.message}`, "error", 8000);
  } finally {
    box.remove();
    bulkRun = null;
    refreshBadge();
    refreshNames();
    emit("review-saved", {});
    if (q.since !== null) load();
  }
}

async function acceptBulk() {
  if (bulkRun) return toast("Accept all is already running (see the box in the corner)", "", 3000);
  const filters = readFilters();
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) if (v) params.set(k, v);
  let counts;
  try {
    counts = await api(`/review/counts?${params}`);
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  if (!counts.total) return toast("Nothing pending under these filters", "", 2000);
  const described = Object.entries(filters)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k.replace("_id", "")} ${v}`)
    .join(", ");
  const go = el(
    "button",
    {
      class: "primary",
      onclick: () => {
        dialog.close();
        runBulkAccept(filters, counts);
      },
    },
    `Accept ${counts.total} as read`
  );
  const dialog = modal(
    "Accept the queue as read?",
    el(
      "div",
      {},
      el("p", {}, `${counts.total} pending item(s)${described ? ` (${described})` : ""} will keep the values the engine read and leave the queue.`),
      el("p", { class: "small muted" }, "Nothing is deleted. Each item is recorded as accepted in bulk, with your name and the time, so it can be traced and changed later in Results. Items that arrive after you opened this dialog stay in the queue."),
      el("p", { class: "small muted" }, "It runs in the background: you can keep working, and stop it from the progress box.")
    ),
    [go]
  );
}

// Small crops (a single bubble row) are upscaled so they are easy to read
function cropImage(src, alt) {
  const img = el("img", { src, alt });
  img.addEventListener("load", () => {
    const box = img.parentElement;
    if (!box) return;
    const maxW = box.clientWidth - 16;
    const maxH = Math.min(window.innerHeight * 0.52, 420);
    const scale = Math.min(4, maxW / img.naturalWidth, maxH / img.naturalHeight);
    if (scale > 1) img.style.width = `${Math.round(img.naturalWidth * scale)}px`;
  });
  return img;
}
