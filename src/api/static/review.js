// Review queue: keyboard-driven, optimistic, prefetching.
import { api, chip, confidenceColor, el, on, toast, url } from "./api.js";

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
};

const key = (item) => `${item.scan_id}/${item.name}`;

export function initReview() {
  document.getElementById("rv-load").addEventListener("click", () => load());
  document.getElementById("rv-template").addEventListener("change", refreshNames);
  document.getElementById("rv-wide").addEventListener("change", () => show());
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
    select.innerHTML = "";
    select.append(el("option", { value: "" }, `All (${summary.total})`));
    for (const row of summary.by_name) select.append(el("option", { value: row.name }, `${row.name} (${row.n})`));
    if ([...select.options].some((o) => o.value === current)) select.value = current;
  } catch (e) {
    /* ignore */
  }
}

function readFilters() {
  const value = (id) => document.getElementById(id).value.trim();
  return {
    template_id: value("rv-template"),
    name: value("rv-name"),
    kind: value("rv-kind"),
    job_id: value("rv-job"),
    scan_id: value("rv-scan"),
  };
}

async function load() {
  Object.assign(q, { buffer: [], pos: 0, seen: new Set(), skipped: new Set(), done: 0, exhausted: false });
  q.filters = readFilters();
  await fetchMore();
  show();
}

async function fetchMore() {
  if (q.loading || q.exhausted) return;
  q.loading = true;
  try {
    const params = new URLSearchParams({ limit: "100", offset: String(q.skipped.size) });
    for (const [k, v] of Object.entries(q.filters)) if (v) params.set(k, v);
    const data = await api(`/review?${params}`);
    q.total = data.total;
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
  const remaining = Math.max(q.total - q.done, 0);
  document.getElementById("rv-progress").textContent = `Reviewed ${q.done} this session · ${remaining} pending · ${q.skipped.size} skipped`;
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
        (item.reasons || []).length ? el("div", { class: "rv-meta rv-reasons" }, item.reasons.join("; ")) : null,
        item.fields ? el("div", { class: "rv-meta" }, "Columns: ", el("code", {}, item.fields.join(", ")), " (space = blank column)") : null,
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
  const firstTime = item.decided === undefined;
  item.decided = value;
  if (firstTime) q.done++;
  q.inFlight++;
  api(`/scans/${item.scan_id}/review`, { method: "POST", json: body })
    .catch((error) => {
      toast(`${item.name} on ${item.file_id}: ${error.message}`, "error", 6000);
      if (firstTime) q.done--;
      delete item.decided;
    })
    .finally(() => {
      q.inFlight--;
      if (q.inFlight === 0) refreshBadge();
      updateProgress();
    });
  move(1);
}

function move(delta, skip = false) {
  const item = current();
  if (skip && item && item.decided === undefined) q.skipped.add(key(item));
  q.pos = Math.max(0, Math.min(q.pos + delta, q.buffer.length));
  show();
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
