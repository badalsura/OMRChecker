// Compare tab: reconcile our results with another reader's export (a physical
// OMR scanner such as Scantron OpScan 6, or any CSV / fixed-width file).
// Mismatches only, one sheet at a time, keyboard first:
//   1 external value · 2 our value · E type a value · R undo / previous field
//   S next mismatch
//   Tab skip sheet · Shift+Tab previous sheet · Enter save and next
//   O aligned / original / colour page · V open the sheet in Results
// Choices go to the reconciled layer on the server; with the rule "save to our
// results" they also become corrections of our results. A comparison runs in
// the background: review starts on the first sheets while a counter shows the rest.
import { api, chip, confidenceColor, displayName, el, emit, modal, on, safeStorage, saveAs, state, toast, url } from "./api.js";

const VIEWS = ["aligned", "original", "color"];
const VIEW_NAMES = { aligned: "Aligned", original: "Original scan", color: "Colour" };

const c = {
  sessions: [],
  session: null,
  sheet: null,
  image: null,
  idx: 0,
  choices: {},
  history: [],
  busy: false,
  view: "aligned",
  poll: null, // timer while a comparison runs in the background
  away: null, // scan_id opened in Results: compared again on return
};

export function initCompare() {
  document.getElementById("cmp-new").addEventListener("click", newComparison);
  document.getElementById("cmp-rules").addEventListener("click", editRules);
  document.getElementById("cmp-rebuild").addEventListener("click", rebuild);
  document.getElementById("cmp-delete").addEventListener("click", removeSession);
  document.getElementById("cmp-export").addEventListener("click", exportMenu);
  document.getElementById("cmp-session").addEventListener("change", (e) => openSession(e.target.value));
  document.addEventListener("keydown", onKey);
  // Template names arrive after the tab may have opened
  on("templates", () => {
    if (tabOpen()) loadSessions();
  });
}

export function onShowCompare() {
  // Back from Results with a sheet: compare it again (it may be regraded)
  if (c.away && c.session && c.sheet && c.sheet.scan_id === c.away) {
    c.away = null;
    return recompare();
  }
  c.away = null;
  loadSessions(true);
}

function tabOpen() {
  const tab = document.getElementById("tab-compare");
  return tab && tab.classList.contains("active");
}

async function loadSessions(reopen = false) {
  try {
    c.sessions = (await api("/compare/sessions")).sessions;
  } catch (error) {
    return toast(error.message, "error");
  }
  const select = document.getElementById("cmp-session");
  const wanted = (c.session && c.session.id) || safeStorage("get", "omr_cmp_session") || "";
  select.innerHTML = "";
  if (!c.sessions.length) select.append(el("option", { value: "" }, "No comparisons yet"));
  for (const s of c.sessions) {
    const template = state.templates.find((t) => t.id === s.template_id);
    const left = s.progress.open;
    select.append(el("option", { value: s.id }, `${s.name} · ${displayName(template && template.name, s.template_id)} · ${left ? `${left} sheets open` : "finished"}`));
  }
  const pick = c.sessions.find((s) => s.id === wanted) || c.sessions[0];
  select.value = pick ? pick.id : "";
  syncButtons();
  if (!pick) return showEmpty("Start a new comparison: pick a template and the other reader's file.");
  if (reopen || !c.session || c.session.id !== pick.id) openSession(pick.id);
}

function syncButtons() {
  const has = Boolean(document.getElementById("cmp-session").value);
  for (const id of ["cmp-rules", "cmp-rebuild", "cmp-delete", "cmp-export"]) document.getElementById(id).disabled = !has;
}

async function openSession(id) {
  syncButtons();
  if (!id) return;
  safeStorage("set", "omr_cmp_session", id);
  try {
    c.session = await api(`/compare/sessions/${id}`);
  } catch (error) {
    return toast(error.message, "error");
  }
  c.sheet = null;
  watchBuild();
  await claim({});
}

// While a comparison runs: a live counter, and the first sheets as they come
function watchBuild() {
  clearTimeout(c.poll);
  c.poll = null;
  if (!c.session || !c.session.building) return;
  const id = c.session.id;
  c.poll = setTimeout(async () => {
    if (!c.session || c.session.id !== id) return;
    try {
      const fresh = await api(`/compare/sessions/${id}`);
      if (!c.session || c.session.id !== id) return;
      const finished = !fresh.building;
      c.session = fresh;
      if (finished) {
        const s = fresh.summary || {};
        if (s.error) toast(`Comparing stopped: ${s.error}`, "error", 8000);
        else toast(`Compared ${s.sheets} sheets in ${s.seconds} s: ${s.mismatches} mismatches on ${(s.states || {}).pending || 0} sheets`, "ok", 5000);
        loadSessions();
      }
      if (!c.sheet && !c.busy) await claim({});
      else progressLine();
    } catch (error) {
      /* try again on the next tick */
    }
    watchBuild();
  }, 1200);
}

function showEmpty(text, extra) {
  const main = document.getElementById("cmp-main");
  main.innerHTML = "";
  main.append(el("div", { class: "empty-state" }, text));
  if (extra) main.append(extra);
  progressLine();
}

// ---------------------------------------------------------------------------
// moving between sheets
// ---------------------------------------------------------------------------
async function claim(body) {
  if (!c.session || c.busy) return;
  c.busy = true;
  try {
    const data = await api(`/compare/sessions/${c.session.id}/claim`, { method: "POST", json: body });
    if (!data.sheet) {
      c.sheet = null;
      if (data.progress) c.session.progress = data.progress;
      const summary = c.session.summary || {};
      const left = data.progress ? data.progress.open : 0;
      if (c.session.building) return showEmpty(left ? `The ${left} open sheet${left === 1 ? " is" : "s are"} being worked on by someone else; more come as the comparison goes on.` : "Comparing… the first sheets with a mismatch show here as soon as they are found.");
      return showEmpty(
        summary.error ? `Comparing stopped: ${summary.error}. Use Compare again.` : left ? `The ${left} open sheet${left === 1 ? " is" : "s are"} being worked on by someone else.` : summary.mismatches ? "Every mismatch is reconciled. Export the reconciled data." : "No mismatches: the two readers agree on every compared field.",
        el("div", { class: "row gap", style: { justifyContent: "center" } }, exportButtons())
      );
    }
    showSheet(data.sheet);
  } catch (error) {
    toast(error.message, "error", 5000);
  } finally {
    c.busy = false;
  }
}

function next() {
  return claim({ after: c.sheet ? c.sheet.ord : null, direction: "next" });
}

async function skipSheet(back = false) {
  if (!c.sheet) return claim({});
  const unsaved = changedDecisions();
  if (Object.keys(unsaved).length) toast(`Skipped without saving ${Object.keys(unsaved).length} choice${Object.keys(unsaved).length === 1 ? "" : "s"}`, "", 2500);
  await claim({ after: c.sheet.ord, direction: back ? "prev" : "next" });
}

// A choice: "external", "ours", null or {source: "manual", value}
function choiceKey(choice) {
  return choice && typeof choice === "object" ? `manual:${choice.value}` : choice || "";
}

function choiceSource(choice) {
  return choice && typeof choice === "object" ? choice.source : choice;
}

function choiceValue(item, choice) {
  const source = choiceSource(choice);
  if (source === "manual") return choice.value;
  return source ? item[source] : null;
}

function changedDecisions() {
  const out = {};
  for (const item of c.sheet ? c.sheet.items : []) {
    const choice = c.choices[item.name] || null;
    const saved = item.source === "manual" ? { source: "manual", value: item.chosen || "" } : item.source || null;
    if (choiceKey(choice) !== choiceKey(saved)) out[item.name] = choice;
  }
  return out;
}

async function saveAndNext() {
  if (!c.sheet || c.busy) return;
  c.busy = true;
  try {
    const data = await api(`/compare/sessions/${c.session.id}/sheets/${c.sheet.scan_id}`, {
      method: "POST",
      json: { decisions: changedDecisions() },
    });
    c.session.progress = data.progress;
    if (data.not_written && Object.keys(data.not_written).length) {
      const names = Object.keys(data.not_written);
      toast(`Not written to our results: ${names.join(", ")} (${Object.values(data.not_written)[0]}). Type the value with E.`, "error", 8000);
    } else if (data.state !== "done") toast(`Saved; ${c.sheet.items.filter((i) => !c.choices[i.name]).length} mismatch(es) on this sheet left open`, "", 2500);
  } catch (error) {
    toast(error.message, "error", 5000);
    return;
  } finally {
    c.busy = false;
  }
  await next();
}

// ---------------------------------------------------------------------------
// one sheet
// ---------------------------------------------------------------------------
function showSheet(sheet) {
  c.sheet = sheet;
  c.session.progress = sheet.progress;
  c.choices = Object.fromEntries(sheet.items.map((item) => [item.name, item.source === "manual" ? { source: "manual", value: item.chosen || "" } : item.source || null]));
  c.history = [];
  const firstOpen = sheet.items.findIndex((item) => !item.source);
  c.idx = firstOpen >= 0 ? firstOpen : 0;
  const main = document.getElementById("cmp-main");
  main.innerHTML = "";
  // The aligned page feeds the zoom; the page shown follows the O toggle
  const img = el("img", { class: "cmp-img", alt: sheet.file_name || "sheet", src: url(sheet.page) });
  c.image = img;
  const shownImg = el("img", { class: "cmp-img cmp-alt", alt: "", hidden: true });
  const inner = el("div", { class: "cmp-page-inner" }, img, shownImg);
  for (const [i, item] of sheet.items.entries()) {
    const box = boxStyle(item.box);
    if (box) inner.append(el("div", { class: "cmp-box", "data-i": i, style: box, title: item.name, onclick: () => go(i) }));
  }
  main.append(
    el(
      "div",
      { class: "cmp-review" },
      el("div", { class: "cmp-page" }, inner),
      el(
        "div",
        { class: "cmp-side" },
        el("div", { class: "cmp-file" }, el("strong", {}, sheet.file_name || sheet.scan_id.slice(0, 8)), sheet.key ? el("span", { class: "muted" }, ` · key ${sheet.key}`) : null, sheet.row_no ? el("span", { class: "muted" }, ` · their row ${sheet.row_no}`) : null),
        el(
          "div",
          { class: "row gap wrap" },
          el("button", { class: "small", id: "cmp-view", onclick: () => cycleView(), title: "Show the aligned page, the original scan or the page in colour (O)" }, el("kbd", {}, "O"), ` ${VIEW_NAMES[c.view]}`),
          el("button", { class: "small", onclick: openInResults, title: "Open this sheet in Results to correct or regrade it; back here it is compared again (V)" }, el("kbd", {}, "V"), " Open in Results"),
          sheet.save_to === "results" ? el("span", { class: "chip", title: "This comparison writes each choice to our results as a correction" }, "saves to our results") : null
        ),
        sheet.state === "done" ? el("div", { class: "rv-meta" }, `Reconciled by ${sheet.done_by || "someone"}: saving changes it.`) : null,
        el("canvas", { class: "cmp-zoom", width: 420, height: 150 }),
        el("div", { id: "cmp-current" }),
        el("div", { class: "cmp-list", id: "cmp-list" }),
        el(
          "div",
          { class: "cmp-actions" },
          el("button", { onclick: () => choose("external"), title: "Keep the other reader's value (1)" }, el("kbd", {}, "1"), " External"),
          el("button", { onclick: () => choose("ours"), title: "Keep our value (2)" }, el("kbd", {}, "2"), " Ours"),
          el("button", { onclick: editValue, title: "Type the right value yourself (E)" }, el("kbd", {}, "E"), " Type value"),
          el("button", { onclick: undo, title: "Undo the last choice, or go back a field (R)" }, el("kbd", {}, "R"), " Undo"),
          el("button", { onclick: () => go(c.idx + 1), title: "Next mismatch on this sheet (S)" }, el("kbd", {}, "S"), " Next mismatch"),
          el("button", { onclick: () => skipSheet(false), title: "Skip this sheet without saving (Tab)" }, el("kbd", {}, "Tab"), " Skip sheet"),
          el("button", { onclick: () => skipSheet(true), title: "Previous sheet (Shift+Tab)" }, el("kbd", {}, "⇧Tab"), " Previous sheet"),
          el("button", { class: "primary", onclick: saveAndNext, title: "Save the choices and go to the next sheet (Enter)" }, el("kbd", {}, "Enter"), " Save and next")
        )
      )
    )
  );
  img.addEventListener("load", () => {
    drawZoom();
    focusBox();
  });
  if (c.view !== "aligned") showView();
  render();
  main.focus({ preventScroll: true });
}

function cycleView() {
  c.view = VIEWS[(VIEWS.indexOf(c.view) + 1) % VIEWS.length];
  showView();
}

// The original scan has its own coordinates: no boxes on it (the zoom keeps
// showing the field on the aligned page)
async function showView() {
  const button = document.getElementById("cmp-view");
  if (button) button.lastChild.textContent = ` ${VIEW_NAMES[c.view]}`;
  const alt = document.querySelector(".cmp-alt");
  if (!alt || !c.sheet) return;
  const aligned = c.view === "aligned";
  c.image.hidden = !aligned;
  alt.hidden = aligned;
  for (const node of document.querySelectorAll(".cmp-box")) node.hidden = c.view === "original";
  if (aligned) return;
  const scanId = c.sheet.scan_id;
  try {
    const data = await api(`/scans/${scanId}/views/${c.view}`);
    if (c.sheet && c.sheet.scan_id === scanId) alt.src = url(data.image_url);
  } catch (error) {
    toast(error.message, "error", 5000);
  }
}

function openInResults() {
  if (!c.sheet) return;
  const item = c.sheet.items[c.idx];
  c.away = c.sheet.scan_id;
  emit("results-scan", { id: c.sheet.scan_id, field: item ? item.name : null });
}

// This sheet compared again with our current values (choices that still apply stay)
async function recompare() {
  if (!c.sheet || c.busy) return;
  const scanId = c.sheet.scan_id;
  const ord = c.sheet.ord;
  const unsaved = changedDecisions();
  c.busy = true;
  let data;
  try {
    data = await api(`/compare/sessions/${c.session.id}/sheets/${scanId}/recompare`, { method: "POST" });
  } catch (error) {
    c.busy = false;
    return toast(error.message, "error", 5000);
  }
  c.busy = false;
  if (!data.sheet) {
    toast("Compared again: this sheet now agrees with their file", "ok", 3000);
    c.session.progress = data.progress;
    return claim({ after: ord, direction: "next" });
  }
  const before = c.sheet.items.length;
  showSheet(data.sheet);
  // Choices not saved yet stay on the fields that still differ
  for (const [name, choice] of Object.entries(unsaved)) if (c.sheet.items.some((i) => i.name === name)) c.choices[name] = choice;
  render();
  toast(`Compared again: ${data.sheet.items.length} mismatch${data.sheet.items.length === 1 ? "" : "es"} (was ${before})`, "", 3000);
}

function boxStyle(box) {
  const size = c.sheet.page_size;
  if (!box || !size) return null;
  const [x, y, w, h] = box;
  const pad = 4;
  return {
    left: `${((x - pad) / size[0]) * 100}%`,
    top: `${((y - pad) / size[1]) * 100}%`,
    width: `${((w + 2 * pad) / size[0]) * 100}%`,
    height: `${((h + 2 * pad) / size[1]) * 100}%`,
  };
}

function shown(value) {
  return value === "" || value === null || value === undefined ? el("span", { class: "muted" }, "(blank)") : value;
}

function render() {
  if (!c.sheet) return;
  const items = c.sheet.items;
  const item = items[c.idx];
  for (const node of document.querySelectorAll(".cmp-box")) {
    const i = Number(node.dataset.i);
    node.classList.toggle("current", i === c.idx);
    node.classList.toggle("decided", Boolean(c.choices[items[i].name]));
  }
  const current = document.getElementById("cmp-current");
  current.innerHTML = "";
  if (item) {
    const choice = choiceSource(c.choices[item.name]);
    current.append(
      el("div", { class: "row gap wrap" }, el("h3", {}, item.name), el("span", { class: "muted small" }, `mismatch ${c.idx + 1} of ${items.length}`)),
      el(
        "div",
        { class: "row gap wrap" },
        el("span", { class: "conf", style: { background: confidenceColor(item.confidence) } }, `conf ${item.confidence === null || item.confidence === undefined ? "–" : Number(item.confidence).toFixed(2)}`),
        (item.flags || []).map((f) => chip(f, "flag"))
      ),
      el(
        "div",
        { class: "cmp-values" },
        el("button", { class: `cmp-value${choice === "external" ? " on" : ""}`, onclick: () => choose("external") }, el("span", { class: "k" }, "1 · External"), el("span", { class: "v" }, shown(item.external))),
        el("button", { class: `cmp-value${choice === "ours" ? " on" : ""}`, onclick: () => choose("ours") }, el("span", { class: "k" }, "2 · Ours"), el("span", { class: "v" }, shown(item.ours)))
      ),
      el("div", { id: "cmp-edit" })
    );
    if (choice === "manual") current.append(el("div", { class: "cmp-manual" }, "Typed: ", el("code", {}, shown(c.choices[item.name].value))));
  }
  const list = document.getElementById("cmp-list");
  list.innerHTML = "";
  for (const [i, it] of items.entries()) {
    const choice = choiceSource(c.choices[it.name]);
    list.append(
      el(
        "div",
        { class: `cmp-row${i === c.idx ? " current" : ""}`, onclick: () => go(i) },
        el("code", {}, it.name),
        el("span", { class: choice === "external" ? "picked" : "" }, shown(it.external)),
        el("span", { class: choice === "ours" ? "picked" : "" }, shown(it.ours)),
        el("span", { class: choice === "manual" ? "picked" : "muted small" }, choice === "manual" ? shown(c.choices[it.name].value) : choice || "open")
      )
    );
  }
  progressLine();
  drawZoom();
}

function drawZoom() {
  const canvas = document.querySelector(".cmp-zoom");
  const img = c.image;
  const item = c.sheet && c.sheet.items[c.idx];
  if (!canvas || !img || !img.naturalWidth || !item || !item.box || !c.sheet.page_size) return;
  const scale = img.naturalWidth / c.sheet.page_size[0];
  const [x, y, w, h] = item.box.map((v) => v * scale);
  const pad = Math.max(16, h * 0.8);
  const sx = Math.max(0, x - pad);
  const sy = Math.max(0, y - pad);
  const sw = Math.min(img.naturalWidth - sx, w + 2 * pad);
  const sh = Math.min(img.naturalHeight - sy, h + 2 * pad);
  const fit = Math.min(canvas.width / sw, canvas.height / sh);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const dx = (canvas.width - sw * fit) / 2;
  const dy = (canvas.height - sh * fit) / 2;
  ctx.drawImage(img, sx, sy, sw, sh, dx, dy, sw * fit, sh * fit);
  ctx.strokeStyle = "#ff8c00";
  ctx.lineWidth = 2;
  ctx.strokeRect(dx + (x - sx) * fit - 3, dy + (y - sy) * fit - 3, w * fit + 6, h * fit + 6);
}

function focusBox() {
  const node = document.querySelector(`.cmp-box[data-i="${c.idx}"]`);
  if (node) node.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
}

function go(i) {
  if (!c.sheet || !c.sheet.items.length) return;
  const n = c.sheet.items.length;
  c.idx = ((i % n) + n) % n;
  render();
  focusBox();
}

function choose(source) {
  const item = c.sheet && c.sheet.items[c.idx];
  if (!item) return;
  c.history.push({ name: item.name, previous: c.choices[item.name] || null, idx: c.idx });
  c.choices[item.name] = source;
  // On to the next open mismatch of the sheet (or stay on the last)
  const items = c.sheet.items;
  for (let step = 1; step <= items.length; step++) {
    const i = (c.idx + step) % items.length;
    if (!c.choices[items[i].name]) return go(i);
  }
  render();
}

// E: type the value; Enter keeps it, Esc goes back to the keys
function editValue() {
  const item = c.sheet && c.sheet.items[c.idx];
  const box = document.getElementById("cmp-edit");
  if (!item || !box) return;
  const current = c.choices[item.name];
  const input = el("input", { class: "rv-value", spellcheck: "false", autocomplete: "off", value: choiceSource(current) ? choiceValue(item, current) || "" : item.external || "" });
  box.innerHTML = "";
  box.append(input, el("div", { class: "rv-meta" }, "Enter keeps this value · Esc cancels · empty = blank"));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      choose({ source: "manual", value: input.value.trim() });
      document.getElementById("cmp-main").focus({ preventScroll: true });
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      box.innerHTML = "";
      document.getElementById("cmp-main").focus({ preventScroll: true });
    }
  });
  input.focus();
  input.select();
}

function undo() {
  if (!c.sheet) return;
  const last = c.history.pop();
  if (last) {
    c.choices[last.name] = last.previous;
    c.idx = last.idx;
    render();
    return focusBox();
  }
  if (c.idx > 0) return go(c.idx - 1);
  toast("Nothing to undo on this sheet", "", 1500);
}

function progressLine() {
  const node = document.getElementById("cmp-progress");
  if (!node) return;
  const p = c.session && c.session.progress;
  if (!p) return (node.textContent = "");
  const parts = [];
  const b = c.session.building;
  if (b) parts.push(b.total ? `Comparing ${b.done.toLocaleString()} of ${b.total.toLocaleString()} sheets…` : "Comparing…");
  if (c.sheet) parts.push(`Sheet ${c.sheet.position} of ${p.to_review}${b ? " so far" : ""}`);
  parts.push(`${p.open} open`, `decided ${p.decided} of ${p.mismatches} mismatches`);
  if (p.chose_external) parts.push(`${p.chose_external} external kept`);
  const summary = c.session.summary || {};
  const states = p.sheets || {};
  if (states.clean) parts.push(`${states.clean} agree`);
  const missing = states.no_record || 0;
  if (p.unmatched_external || missing) parts.push(`unmatched: ${p.unmatched_external} of theirs, ${missing} of ours`);
  if (states.replaced) parts.push(`${states.replaced} older scans of a file read again`);
  if (states.duplicate) parts.push(`${states.duplicate} not compared (same file in several scans)`);
  if (states.not_read) parts.push(`${states.not_read} not read`);
  node.textContent = parts.join(" · ");
}

function onKey(e) {
  if (!tabOpen() || !c.sheet || document.querySelector(".modal-backdrop")) return;
  const target = e.target;
  if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const key = e.key;
  if (key === "Tab") {
    e.preventDefault();
    skipSheet(e.shiftKey);
  } else if (key === "Enter") {
    e.preventDefault();
    saveAndNext();
  } else if (key === "1" || e.code === "Digit1" || e.code === "Numpad1") {
    e.preventDefault();
    choose("external");
  } else if (key === "2" || e.code === "Digit2" || e.code === "Numpad2") {
    e.preventDefault();
    choose("ours");
  } else if (key === "e" || key === "E") {
    e.preventDefault();
    editValue();
  } else if (key === "r" || key === "R") {
    e.preventDefault();
    undo();
  } else if (key === "s" || key === "S") {
    e.preventDefault();
    go(c.idx + 1);
  } else if (key === "o" || key === "O") {
    e.preventDefault();
    cycleView();
  } else if (key === "v" || key === "V") {
    e.preventDefault();
    openInResults();
  }
}

// ---------------------------------------------------------------------------
// session actions
// ---------------------------------------------------------------------------
async function rebuild() {
  if (!c.session) return;
  try {
    c.session = await api(`/compare/sessions/${c.session.id}/rebuild`, { method: "POST" });
    if (!c.session.building) toast(`Compared again: ${c.session.summary.mismatches} mismatches`, "ok");
    watchBuild();
    await claim({});
  } catch (error) {
    toast(error.message, "error", 6000);
  }
}

async function removeSession() {
  if (!c.session) return;
  if (!confirm(`Delete the comparison "${c.session.name}" and every choice made in it? Read results are not touched.`)) return;
  try {
    await api(`/compare/sessions/${c.session.id}`, { method: "DELETE" });
    c.session = null;
    c.sheet = null;
    safeStorage("set", "omr_cmp_session", "");
    await loadSessions(true);
  } catch (error) {
    toast(error.message, "error", 6000);
  }
}

function exportButtons() {
  if (!c.session) return [];
  const base = `/compare/sessions/${c.session.id}/export?kind=`;
  const stem = c.session.name.replace(/[^\w.-]+/g, "_");
  return [
    el("button", { class: "primary", onclick: () => saveAs(base + "reconciled", `${stem}_reconciled.csv`), title: "Every sheet with our values, the chosen value wherever someone decided, and what is still open" }, "Reconciled CSV"),
    el("button", { onclick: () => saveAs(base + "audit", `${stem}_audit.csv`), title: "Every decision: who, when, both values and which was kept" }, "Audit CSV"),
    el("button", { onclick: () => saveAs(base + "reconciled&matched=true", `${stem}_reconciled_matched.csv`), title: "Only the sheets that found a row in their file" }, "Reconciled CSV (matched sheets only)"),
    el("button", { onclick: () => saveAs(base + "unmatched", `${stem}_unmatched.csv`), title: "Their rows with no sheet, and our sheets with no row" }, "Unmatched CSV"),
  ];
}

function exportMenu() {
  if (!c.session) return;
  const dialog = modal("Export", el("div", { class: "row gap wrap" }, exportButtons()), [el("button", { onclick: () => dialog.close() }, "Close")]);
}

// ---------------------------------------------------------------------------
// new comparison and rules
// ---------------------------------------------------------------------------
function codes(text) {
  return String(text || "")
    .split(",")
    .map((part) => (part.trim().toLowerCase() === "space" ? " " : part.trim()))
    .filter((part) => part !== "" || false);
}

function codesText(list) {
  return (list || []).map((code) => (code === " " ? "space" : code)).join(", ");
}

function rulesForm(rules) {
  const f = {
    fields: el("select", {}, el("option", { value: "all" }, "Every mapped column"), el("option", { value: "questions" }, "Questions only (bubble fields with letter options)"), el("option", { value: "list" }, "Only these:")),
    list: el("input", { placeholder: "q1..q50, Roll", size: 24, value: rules.list || "" }),
    blank: el("select", {}, el("option", { value: "mismatch" }, "is a mismatch"), el("option", { value: "ignore" }, "is ignored")),
    multi: el("select", {}, el("option", { value: "same" }, "equal any other multi-mark"), el("option", { value: "exact" }, "must mark the same options"), el("option", { value: "ignore" }, "are ignored")),
    case: el("input", { type: "checkbox", checked: Boolean(rules.case) }),
    spaces: el("input", { type: "checkbox", checked: rules.spaces !== false }),
    blank_codes: el("input", { size: 12, value: codesText(rules.blank_codes) }),
    multi_codes: el("input", { size: 8, value: codesText(rules.multi_codes) }),
    band_field: el("input", { size: 10, value: rules.band_field || "", placeholder: "e.g. level", spellcheck: "false" }),
    band_source: el("select", {}, el("option", { value: "theirs_then_ours" }, "theirs, else ours"), el("option", { value: "theirs" }, "their value"), el("option", { value: "ours" }, "our value"), el("option", { value: "either" }, "either (both bands)")),
    band_map: el("input", { size: 46, value: rules.band_map || "", placeholder: "1: q1..q50; 2: q51..q100; 3: q101..q150; 4: q151..q200", spellcheck: "false" }),
    save_to: el("select", {}, el("option", { value: "layer" }, "a new reconciled layer (our results stay as read)"), el("option", { value: "results" }, "our results, in place (as corrections, undoable)")),
    duplicates: el("select", {}, el("option", { value: "newest" }, "compare its newest scan"), el("option", { value: "skip" }, "compare none of them")),
  };
  f.save_to.value = rules.save_to || "layer";
  f.duplicates.value = rules.duplicates || "newest";
  f.fields.value = rules.fields || "all";
  f.blank.value = rules.blank || "mismatch";
  f.multi.value = rules.multi || "same";
  f.band_source.value = rules.band_source || "theirs_then_ours";
  const node = el(
    "div",
    { class: "cmp-form" },
    el("label", { class: "field inline" }, "Compare ", f.fields, " ", f.list),
    el("label", { class: "field inline" }, "One side blank, the other marked ", f.blank),
    el("label", { class: "field inline" }, "Multi-marks ", f.multi),
    el("label", { class: "field inline", title: "What their file writes for an unmarked answer; 'space' is a single space" }, "Their blank is written as ", f.blank_codes),
    el("label", { class: "field inline", title: "What their file writes when more than one option is marked" }, "Their multi-mark is written as ", f.multi_codes),
    el("label", { class: "field inline" }, f.case, " Upper and lower case differ"),
    el("label", { class: "field inline", title: "A roll number with a blank digit: '12 45' equals '1245'" }, f.spaces, " Ignore spaces inside values"),
    el("label", { class: "field inline", title: "When the same file was read in several jobs" }, "A file we read more than once: ", f.duplicates),
    el("label", { class: "field inline", title: "In place: each choice also corrects our result (audited; undo it in Results), so Results and every export show it" }, "Save choices to ", f.save_to),
    el(
      "details",
      { open: Boolean(rules.band_field) },
      el("summary", { class: "small" }, "Compare only some questions per sheet (level bands)"),
      el(
        "div",
        { class: "row gap wrap", title: "Each sheet answers only the questions of its level: questions outside that band are not compared (a scanner can report stray marks there). A sheet whose level is blank or not listed is compared in full." },
        el("span", { class: "small" }, "Only the questions of the level in field"),
        f.band_field,
        el("span", { class: "small" }, "taken from"),
        f.band_source,
        el("span", { class: "small" }, "bands"),
        f.band_map
      )
    )
  );
  const read = () => ({
    fields: f.fields.value,
    list: f.list.value,
    blank: f.blank.value,
    multi: f.multi.value,
    case: f.case.checked,
    spaces: f.spaces.checked,
    blank_codes: codes(f.blank_codes.value),
    multi_codes: codes(f.multi_codes.value),
    band_field: f.band_field.value.trim(),
    band_source: f.band_source.value,
    band_map: f.band_map.value.trim(),
    save_to: f.save_to.value,
    duplicates: f.duplicates.value,
  });
  return { node, read };
}

function keyForm(preview, key) {
  const column = el("select", {}, preview.columns.map((name) => el("option", { value: name }, name)));
  const ours = el(
    "select",
    {},
    el("option", { value: "file" }, "our file name"),
    el("option", { value: "order" }, "scan order (row n = sheet n)"),
    (preview.imprint_zones || []).map((name) => el("option", { value: name }, `${name} (imprint)`)),
    preview.names.filter((name) => !(preview.imprint_zones || []).includes(name)).map((name) => el("option", { value: name }, name))
  );
  const normalize = el("select", {}, el("option", { value: "digits" }, "digits only (S0123 = 123.jpg)"), el("option", { value: "text" }, "text, any case"), el("option", { value: "exact" }, "exactly"));
  column.value = key.column || preview.columns[0];
  ours.value = key.ours || "file";
  normalize.value = key.normalize || "digits";
  const sync = () => (column.disabled = normalize.disabled = ours.value === "order");
  ours.addEventListener("change", sync);
  sync();
  const node = el("div", { class: "row gap wrap" }, el("label", { class: "field inline" }, "Their column ", column), el("label", { class: "field inline" }, "matches ", ours), el("label", { class: "field inline" }, "compared as ", normalize));
  return { node, read: () => ({ column: column.value, ours: ours.value, normalize: normalize.value }) };
}

async function newComparison() {
  const template = el("select", {});
  template.append(...state.templates.map((t) => el("option", { value: t.id }, displayName(t.name, t.id))));
  const current = document.getElementById("res-template");
  if (current && current.value) template.value = current.value;
  const everyJob = "Every job of the template (a file read again: its newest scan)";
  const job = el("select", {}, el("option", { value: "" }, everyJob));
  // The newest job of the template is picked: one run of the sheets
  const fillJobs = async () => {
    job.innerHTML = "";
    job.append(el("option", { value: "" }, everyJob));
    try {
      const jobs = (await api("/jobs?limit=200")).jobs.filter((j) => !j.template_id || j.template_id === template.value);
      jobs.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
      for (const j of jobs) {
        job.append(el("option", { value: j.id }, `${j.name || j.id.slice(0, 8)} · ${j.total_files || j.processed_files || 0} sheets · ${j.created_at ? new Date(j.created_at * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : ""}`));
      }
      if (jobs.length) job.value = jobs[0].id;
    } catch (error) {
      /* jobs are optional */
    }
  };
  template.addEventListener("change", fillJobs);
  fillJobs();

  // A saved layout fills in every setting below and the mapping
  let profiles = [];
  const profile = el("select", {}, el("option", { value: "" }, "none"));
  const loadProfiles = async () => {
    try {
      profiles = (await api("/compare/profiles")).profiles;
    } catch (error) {
      profiles = [];
    }
    const keep = profile.value;
    profile.innerHTML = "";
    profile.append(el("option", { value: "" }, profiles.length ? "none" : "none saved yet"));
    for (const p of profiles) profile.append(el("option", { value: p.name }, p.name));
    profile.value = keep;
  };
  loadProfiles();

  const file = el("input", { type: "file", accept: ".csv,.txt,.dat,.tsv,.prn" });
  const format = el("select", {}, el("option", { value: "auto" }, "Auto"), el("option", { value: "csv" }, "CSV / delimited"), el("option", { value: "fixed" }, "Fixed width"));
  const delimiter = el("select", {}, el("option", { value: "" }, "auto"), el("option", { value: "," }, ", comma"), el("option", { value: ";" }, "; semicolon"), el("option", { value: "tab" }, "tab"), el("option", { value: "|" }, "| pipe"));
  const header = el("input", { type: "number", min: 0, value: 1, style: { width: "4.5em" }, title: "The line with the column names: 1 = the first line, 0 = the file has no header" });
  const names = el("input", { size: 40, placeholder: "e.g. SEQ, ROLL, q1..q200 (leave empty to use the header)", spellcheck: "false" });
  const fixed = el("textarea", { rows: 4, cols: 40, placeholder: "One column per line: NAME START LENGTH\nROLL 1 12\nq1..q200 13 1", spellcheck: "false" });
  const split = el("textarea", { rows: 3, cols: 40, placeholder: "One packed column per line: COLUMN FIELDS [width=N] [sep=X]\nANSWERS q1..q200\nA1 q1..q10\nA2 q11..q20", spellcheck: "false" });
  const fixedRow = el("label", { class: "field" }, "Fixed-width columns (START counts from 1)", fixed);
  const syncFormat = () => (fixedRow.style.display = format.value === "fixed" ? "" : "none");
  format.addEventListener("change", syncFormat);
  syncFormat();
  const step2 = el("div", {});
  const name = el("input", { placeholder: "e.g. OpScan batch 3", size: 30 });
  let preview = null;
  let parts = null;
  let readName = null;

  const parse = () => ({
    format: format.value,
    delimiter: delimiter.value || null,
    header_row: Number(header.value || 0),
    names: names.value,
    fixed: format.value === "fixed" ? fixed.value : "",
    split: split.value,
  });
  const applyParse = (saved) => {
    format.value = saved.format || "auto";
    delimiter.value = saved.delimiter || "";
    header.value = saved.header_row !== undefined && saved.header_row !== null ? saved.header_row : saved.header === false ? 0 : 1;
    names.value = saved.names || "";
    fixed.value = saved.fixed || "";
    split.value = saved.split || "";
    syncFormat();
  };
  profile.addEventListener("change", () => {
    const chosen = profiles.find((p) => p.name === profile.value);
    if (!chosen) return;
    applyParse(chosen.settings.parse || {});
    if (chosen.template_id && state.templates.some((t) => t.id === chosen.template_id)) {
      template.value = chosen.template_id;
      fillJobs();
    }
    if (preview) readFile();
  });

  const readFile = async () => {
    if (!file.files.length) return toast("Pick their file first");
    if (!template.value) return toast("Pick a template");
    const edited = parts ? parts.mapping() : null;
    try {
      if (preview && readName === file.files[0].name) {
        // Same file: read it again with the new settings (no upload)
        preview = await api(`/compare/preview/${preview.upload}`, { method: "POST", json: { template_id: template.value, parse: parse() } });
      } else {
        const form = new FormData();
        form.append("file", file.files[0]);
        form.append("template_id", template.value);
        form.append("parse", JSON.stringify(parse()));
        preview = await api("/compare/preview", { method: "POST", form });
        readName = file.files[0].name;
      }
    } catch (error) {
      return toast(error.message, "error", 6000);
    }
    if (!name.value) name.value = file.files[0].name.replace(/\.[^.]+$/, "");
    const chosen = profiles.find((p) => p.name === profile.value);
    const settings = chosen ? chosen.settings : {};
    // Keep what was typed in the mapping for columns that are still there
    const mapping = chosen ? { ...(settings.mapping || {}) } : { ...preview.mapping };
    if (edited) for (const [column, value] of Object.entries(edited)) if (preview.columns.includes(column)) mapping[column] = value;
    parts = showPreview(step2, { ...preview, mapping }, settings);
  };

  const create = async () => {
    if (!preview) return readFile();
    const body = {
      upload: preview.upload,
      template_id: template.value,
      job_id: job.value || null,
      name: name.value || null,
      parse: parse(),
      mapping: parts.mapping(),
      key: parts.key.read(),
      rules: parts.rules.read(),
    };
    try {
      const session = await api("/compare/sessions", { method: "POST", json: body });
      dialog.close();
      c.session = session;
      if (!session.building) toast(`${session.summary.matched} sheets matched · ${session.summary.mismatches} mismatches`, "ok", 5000);
      await loadSessions(true);
    } catch (error) {
      toast(error.message, "error", 8000);
    }
  };

  const saveProfile = async () => {
    if (!preview) return toast("Read the file first, then save its layout");
    const title = prompt("Save this layout as (e.g. OpScan TERI layout):", profile.value || "");
    if (!title) return;
    try {
      await api("/compare/profiles", {
        method: "POST",
        json: { name: title.trim(), template_id: template.value, parse: parse(), mapping: parts.mapping(), key: parts.key.read(), rules: parts.rules.read() },
      });
      await loadProfiles();
      profile.value = title.trim();
      toast(`Saved "${title.trim()}"`, "ok");
    } catch (error) {
      toast(error.message, "error", 6000);
    }
  };

  const body = el(
    "div",
    { class: "cmp-form" },
    el("div", { class: "row gap wrap" }, el("label", { class: "field inline" }, "Template ", template), el("label", { class: "field inline" }, "Our sheets ", job), el("label", { class: "field inline", title: "A layout saved earlier: how a file is read, mapped and compared" }, "Saved layout ", profile)),
    el("div", { class: "row gap wrap" }, el("label", { class: "field inline" }, "Their file ", file), el("label", { class: "field inline" }, "Format ", format), el("label", { class: "field inline" }, "Delimiter ", delimiter), el("label", { class: "field inline" }, "Header row ", header)),
    el("label", { class: "field inline", title: "Name the columns yourself: replaces the header, or names a file without one (ranges like q1..q200 allowed)" }, "Column names ", names),
    fixedRow,
    el("label", { class: "field", title: "A column that holds many answers: split it into one column per field" }, "Packed answer columns (optional)", split),
    el("div", { class: "row gap" }, el("button", { onclick: readFile }, "Read file / read again"), el("span", { class: "muted small" }, "Change any setting above and read again to check the split.")),
    step2,
    el("label", { class: "field inline" }, "Name ", name)
  );
  const dialog = modal(
    "New comparison",
    body,
    [el("button", { onclick: saveProfile, title: "Keep these settings and this mapping for the next file of the same kind" }, "Save layout…"), el("button", { onclick: () => dialog.close() }, "Cancel"), el("button", { class: "primary", onclick: create }, "Compare")],
    { wide: true }
  );
}

function showPreview(container, preview, saved = {}) {
  container.innerHTML = "";
  const table = el(
    "table",
    { class: "table small" },
    el("thead", {}, el("tr", {}, preview.columns.map((name) => el("th", {}, name)))),
    el("tbody", {}, preview.rows.map((row) => el("tr", {}, row.map((cell) => el("td", {}, cell === " " ? "␣" : cell)))))
  );
  const list = el("datalist", { id: "cmp-names" }, preview.names.map((n) => el("option", { value: n })));
  const inputs = {};
  const mapping = el("div", { class: "cmp-mapping" });
  for (const column of preview.columns) {
    inputs[column] = el("input", { list: "cmp-names", value: preview.mapping[column] || "", placeholder: "not compared", size: 12 });
    mapping.append(el("label", {}, el("code", {}, column), inputs[column]));
  }
  // Quick fill: their columns in order onto a range of our fields
  const from = el("select", {}, preview.columns.map((name) => el("option", { value: name }, name)));
  const range = el("input", { size: 12, placeholder: "q1..q200" });
  const fill = () => {
    let targets;
    try {
      targets = expandRange(range.value.trim());
    } catch (error) {
      return toast(error.message, "error");
    }
    const at = preview.columns.indexOf(from.value);
    targets.forEach((target, i) => {
      const column = preview.columns[at + i];
      if (column) inputs[column].value = target;
    });
    toast(`Mapped ${Math.min(targets.length, preview.columns.length - at)} columns from ${from.value}`, "ok");
  };
  const clear = () => Object.values(inputs).forEach((input) => (input.value = ""));
  const key = keyForm(preview, saved.key && preview.columns.includes(saved.key.column) ? saved.key : preview.key);
  const rules = rulesForm({ ...preview.rules, ...(saved.rules || {}) });
  const mapped = Object.values(preview.mapping).filter(Boolean).length;
  container.append(
    el("div", { class: "rv-meta" }, `${preview.count} rows in ${preview.file_name}. First rows as read (␣ = a space):`),
    el("div", { class: "cmp-sample" }, table),
    el("h4", {}, "How their rows find our sheets"),
    key.node,
    el("h4", {}, `Columns to compare (${mapped} mapped; type our field for a column, clear a box to leave it out)`),
    el("div", { class: "row gap wrap" }, el("span", { class: "small" }, "Map from column"), from, el("span", { class: "small" }, "onwards to"), range, el("button", { class: "small", onclick: fill }, "Fill"), el("button", { class: "small ghost", onclick: clear }, "Clear all")),
    list,
    mapping,
    el("h4", {}, "Rules"),
    rules.node
  );
  return {
    key,
    rules,
    mapping: () => Object.fromEntries(Object.entries(inputs).map(([column, input]) => [column, input.value.trim() || null])),
  };
}

function expandRange(text) {
  const match = /^([^\d.]*)(\d+)\.{2,3}([^\d.]*)(\d+)$/.exec(text);
  if (!match) {
    if (text && !text.includes(".")) return [text];
    throw new Error("Write a range like q1..q200");
  }
  const [, prefix, start, other, end] = match;
  if (other && other !== prefix) throw new Error("Both ends need the same prefix");
  const out = [];
  for (let n = Number(start); n <= Number(end); n++) out.push(`${prefix}${n}`);
  return out;
}

function editRules() {
  if (!c.session) return;
  const settings = c.session.settings || {};
  const rules = rulesForm(settings.rules || {});
  const preview = { columns: Object.keys(settings.mapping || {}), names: Object.values(settings.mapping || {}), imprint_zones: [] };
  if (settings.key && settings.key.column && !preview.columns.includes(settings.key.column)) preview.columns.unshift(settings.key.column);
  if (settings.key && settings.key.ours && !["file", "order"].includes(settings.key.ours) && !preview.names.includes(settings.key.ours)) preview.names.unshift(settings.key.ours);
  const key = keyForm(preview, settings.key || {});
  const save = async () => {
    try {
      c.session = await api(`/compare/sessions/${c.session.id}`, { method: "PATCH", json: { rules: rules.read(), key: key.read() } });
      dialog.close();
      if (!c.session.building) toast(`Compared again: ${c.session.summary.mismatches} mismatches; choices whose two values did not change are kept`, "ok", 5000);
      watchBuild();
      await claim({});
    } catch (error) {
      toast(error.message, "error", 8000);
    }
  };
  const dialog = modal(
    `Rules: ${c.session.name}`,
    el("div", { class: "cmp-form" }, el("h4", {}, "How their rows find our sheets"), key.node, el("h4", {}, "Rules"), rules.node),
    [el("button", { onclick: () => dialog.close() }, "Cancel"), el("button", { class: "primary", onclick: save }, "Compare again")],
    { wide: true }
  );
}
