// Compare tab: reconcile our results with another reader's export (a physical
// OMR scanner such as Scantron OpScan 6, or any CSV / fixed-width file).
// Mismatches only, one sheet at a time, keyboard first:
//   1 external value · 2 our value · E type a value · R undo / previous field
//   S next mismatch
//   Tab skip sheet · Shift+Tab previous sheet · Enter save and next
//   O aligned / original / colour page · V open the sheet in Results
//   A every compared field side by side, or the mismatches only
// Choices go to the reconciled layer on the server; with the rule "save to our
// results" they also become corrections of our results. A comparison runs in
// the background: review starts on the first sheets while a counter shows the rest.
import { api, chip, confidenceColor, displayName, el, emit, modal, on, safeStorage, saveAs, state, toast, url } from "./api.js";
import { pickProfileFiles, saveProfileFile } from "./profile_files.js";

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
  all: false, // A: every compared field in the list, not just the mismatches
  fields: null, // {scan_id, list} for the all-fields list
  peek: null, // a matching field picked in that list (zoom and page box)
  filter: "",
  alt: null, // {scan_id, view, img, meta} of the colour or original page once loaded
  pinned: null, // {name, external, ours, box} of the field always shown (e.g. rollno)
};

// The field shown on every sheet next to the mismatches; by default the roll
// number when their file has one. "" = none (the choice is remembered).
function pinName() {
  const saved = safeStorage("get", "omr_cmp_pin");
  if (saved !== null && saved !== undefined) return saved;
  const mapped = Object.values((c.session && c.session.settings && c.session.settings.mapping) || {}).filter(Boolean);
  return mapped.find((name) => /^roll/i.test(name)) || "";
}

export function initCompare() {
  document.getElementById("cmp-new").addEventListener("click", newComparison);
  document.getElementById("cmp-rules").addEventListener("click", editRules);
  document.getElementById("cmp-rebuild").addEventListener("click", rebuild);
  document.getElementById("cmp-delete").addEventListener("click", removeSession);
  document.getElementById("cmp-export").addEventListener("click", exportMenu);
  document.getElementById("cmp-session").addEventListener("change", (e) => openSession(e.target.value));
  document.getElementById("cmp-goto").addEventListener("submit", (e) => {
    e.preventDefault();
    gotoSheet();
  });
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
    select.append(el("option", { value: s.id }, `${s.name} · ${displayName(template && template.name, s.template_id)} · ${left ? `${left} sheet${left === 1 ? "" : "s"} left` : "finished"}`));
  }
  const pick = c.sessions.find((s) => s.id === wanted) || c.sessions[0];
  select.value = pick ? pick.id : "";
  syncButtons();
  if (!pick) return showEmpty("Start a new comparison: pick a template and the other reader's file.");
  if (reopen || !c.session || c.session.id !== pick.id) openSession(pick.id);
}

function syncButtons() {
  const has = Boolean(document.getElementById("cmp-session").value);
  for (const id of ["cmp-rules", "cmp-rebuild", "cmp-delete", "cmp-export", "cmp-goto-q"]) document.getElementById(id).disabled = !has;
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
// Jump to one sheet by its key, file name or "#N" (its place among the
// sheets to review); a sheet with nothing to review says why
async function gotoSheet() {
  const input = document.getElementById("cmp-goto-q");
  const text = input.value.trim();
  if (!c.session || !text) return;
  try {
    const found = await api(`/compare/sessions/${c.session.id}/find?q=${encodeURIComponent(text)}`);
    if (c.sheet && c.sheet.scan_id === found.scan_id) return toast("That sheet is already open", "ok");
    await claim({ scan_id: found.scan_id });
    input.value = "";
    input.blur();
  } catch (error) {
    toast(error.message, "error", 7000);
  }
}

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
      if (c.session.building) return showEmpty(left ? `The ${left} sheet${left === 1 ? "" : "s"} left ${left === 1 ? "is" : "are"} open for someone else; more come as the comparison goes on.` : "Comparing… the first sheets with a mismatch show here as soon as they are found.");
      return showEmpty(
        summary.error ? `Comparing stopped: ${summary.error}. Use Compare again.` : left ? `The ${left} sheet${left === 1 ? "" : "s"} left ${left === 1 ? "is" : "are"} open for someone else.` : summary.mismatches ? "Every mismatch is reconciled. Export the reconciled data." : "No mismatches: the two readers agree on every compared field.",
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
    } else if (data.state !== "done") toast(`Saved; ${c.sheet.items.filter((i) => !c.choices[i.name]).length} mismatch(es) on this sheet still undecided`, "", 2500);
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
  c.fields = null;
  c.peek = null;
  c.alt = null;
  c.pinned = null;
  const firstOpen = sheet.items.findIndex((item) => !item.source);
  c.idx = firstOpen >= 0 ? firstOpen : 0;
  const main = document.getElementById("cmp-main");
  main.innerHTML = "";
  // The aligned page feeds the zoom; the page shown follows the O toggle
  const img = el("img", { class: "cmp-img", alt: sheet.file_name || "sheet", src: url(sheet.page) });
  c.image = img;
  const shownImg = el("img", { class: "cmp-img cmp-alt", alt: "", hidden: true });
  const inner = el("div", { class: "cmp-page-inner" }, img, shownImg);
  // Which page is on screen, on the page itself
  const badge = el("div", { class: "cmp-view-badge", id: "cmp-view-badge" }, VIEW_NAMES[c.view]);
  for (const [i, item] of sheet.items.entries()) {
    const box = boxStyle(item.box);
    if (box) inner.append(el("div", { class: "cmp-box", "data-i": i, "data-name": item.name, style: box, title: `${item.name}: theirs ${item.external || "(blank)"} · ours ${item.ours || "(blank)"}`, onclick: () => go(i) }));
  }
  const rb = (key, label, onclick, title, extra = {}) => el("button", { onclick, title, ...extra }, el("kbd", {}, key), el("span", {}, label));
  main.append(
    el(
      "div",
      { class: "cmp-review", id: "cmp-review" },
      el("div", { class: "cmp-page-wrap" }, badge, el("div", { class: "cmp-page" }, inner)),
      el("div", { class: "cmp-split", title: "Drag to make the page or the panel wider (double-click: back to normal)", onpointerdown: dragSplit, ondblclick: () => setLayout({ side: null }) }),
      el(
        "div",
        { class: "cmp-side" },
        el("div", { class: "cmp-file", title: sheet.file_name || sheet.scan_id }, el("strong", {}, sheet.file_name || sheet.scan_id.slice(0, 8)), sheet.key ? el("span", { class: "muted" }, ` · key ${sheet.key}`) : null, sheet.row_no ? el("span", { class: "muted" }, ` · their row ${sheet.row_no}`) : null, sheet.save_to === "results" ? el("span", { class: "chip", title: "This comparison writes each choice to our results as a correction" }, "saves to our results") : null),
        el("div", { id: "cmp-pin", class: "cmp-pin" }),
        sheet.state === "done" ? el("div", { class: "rv-meta" }, `Reconciled by ${sheet.done_by || "someone"}: saving changes it.`) : null,
        el("canvas", { class: "cmp-zoom", width: 420, height: ZOOM_HEIGHT[zoomMode()] }),
        el("div", { class: "muted small", id: "cmp-zoom-view" }),
        el("div", { id: "cmp-current" }),
        el("input", { id: "cmp-filter", class: "small", placeholder: "Show only: q1..q50, roll", value: c.filter, hidden: !c.all, spellcheck: "false", oninput: (e) => { c.filter = e.target.value; render(); } }),
        el("div", { class: `cmp-list${c.all ? " all" : ""}`, id: "cmp-list" })
      ),
      // Every button in one column at the right edge, so the panel keeps its room for the data
      el(
        "div",
        { class: "cmp-ribbon", role: "toolbar", "aria-orientation": "vertical" },
        rb("1", "External", () => choose("external"), "Keep the other reader's value (1)"),
        rb("2", "Ours", () => choose("ours"), "Keep our value (2)"),
        rb("E", "Type value", editValue, "Type the right value yourself (E)"),
        rb("R", "Undo", undo, "Undo the last choice, or go back a field (R)"),
        rb("S", "Next field", () => go(c.idx + 1), "Next mismatch on this sheet (S)"),
        el("hr"),
        rb("Enter", "Save + next", saveAndNext, "Save the choices and go to the next sheet (Enter)", { class: "primary" }),
        rb("Tab", "Skip sheet", () => skipSheet(false), "Skip this sheet without saving (Tab)"),
        rb("⇧Tab", "Previous", () => skipSheet(true), "Previous sheet (Shift+Tab)"),
        el("hr"),
        rb("O", VIEW_NAMES[c.view], cycleView, "Which page is shown: aligned, original scan or colour (O switches)", { id: "cmp-view" }),
        rb("W", zoomButtonText(), toggleWide, "Close-up size: the field alone, the field with its neighbours, or a wide slice of the page (W steps through them)", { id: "cmp-wide" }),
        rb("A", c.all ? "Mismatches" : "All fields", toggleAll, "Every compared field side by side, or the mismatches only (A)", { id: "cmp-all" }),
        rb("V", "In Results", openInResults, "Open this sheet in Results to correct or regrade it; back here it is compared again (V)")
      )
    ),
    el("div", { class: "cmp-grip", title: "Drag to make the page and the panel taller or shorter (double-click: back to normal)", onpointerdown: dragHeight, ondblclick: () => setLayout({ height: null }) })
  );
  applyLayout();
  img.addEventListener("load", () => {
    drawZoom();
    focusBox(false);
  });
  if (c.view !== "aligned") showView();
  if (c.all || pinName()) loadFields();
  render();
  main.focus({ preventScroll: true });
}

function toggleAll() {
  c.all = !c.all;
  c.peek = null;
  const button = document.getElementById("cmp-all");
  if (button) button.lastChild.textContent = c.all ? "Mismatches" : "All fields";
  if (c.all && !c.fields) return loadFields();
  matchBoxes();
  render();
  focusBox();
}

async function loadFields() {
  if (!c.sheet) return;
  const scanId = c.sheet.scan_id;
  const pin = pinName();
  try {
    const data = await api(`/compare/sessions/${c.session.id}/sheets/${scanId}/fields${pin ? `?pin=${encodeURIComponent(pin)}` : ""}`);
    if (!c.sheet || c.sheet.scan_id !== scanId) return;
    c.fields = { scan_id: scanId, list: data.fields };
    c.pinned = data.pinned || null;
  } catch (error) {
    return toast(error.message, "error", 5000);
  }
  matchBoxes();
  render();
  if (c.all) focusBox();
}

// The pinned field: a picker, its two values and a close-up of it
function renderPin() {
  const box = document.getElementById("cmp-pin");
  if (!box || !c.sheet) return;
  box.innerHTML = "";
  const pin = pinName();
  const names = [...new Set([...Object.values((c.session.settings || {}).mapping || {}).filter(Boolean), ...(pin ? [pin] : [])])];
  const picker = el("select", { class: "small", title: "A field shown on every sheet, compared or not (e.g. the roll number)" }, el("option", { value: "" }, "nothing"), names.map((name) => el("option", { value: name }, name)));
  picker.value = pin;
  picker.addEventListener("change", () => {
    safeStorage("set", "omr_cmp_pin", picker.value);
    c.pinned = null;
    if (picker.value) loadFields();
    else renderPin();
  });
  const p = pin && c.pinned && c.pinned.name === pin ? c.pinned : null;
  const head = el("div", { class: "row gap wrap" }, el("span", { class: "muted small" }, "Always show"), picker);
  if (!pin) return box.append(head);
  if (!p) return box.append(head, el("span", { class: "muted small" }, "loading…"));
  const same = p.external !== null && p.external !== undefined && String(p.external).replace(/\s/g, "") === String(p.ours ?? "").replace(/\s/g, "");
  head.append(
    el("span", { class: "cmp-pin-v" }, el("span", { class: "k" }, "theirs "), p.external === null || p.external === undefined ? el("span", { class: "muted" }, "not in their file") : shown(p.external)),
    el("span", { class: "cmp-pin-v" }, el("span", { class: "k" }, "ours "), shown(p.ours)),
    p.external === null || p.external === undefined ? null : el("span", { class: same ? "chip ok" : "chip flag" }, same ? "same" : "differ")
  );
  box.append(head);
  if (p.box && c.image) {
    const canvas = el("canvas", { class: "cmp-pin-zoom", width: 420, height: 70 });
    box.append(canvas);
    const draw = () => drawCrop(canvas, c.image, p.box, c.sheet.page_size);
    if (c.image.naturalWidth) draw();
    else c.image.addEventListener("load", draw, { once: true });
  }
}

function drawCrop(canvas, img, box, size) {
  if (!img.naturalWidth || !box || !size) return;
  const scale = [img.naturalWidth / size[0], img.naturalHeight / size[1]];
  const [x, y, w, h] = [box[0] * scale[0], box[1] * scale[1], box[2] * scale[0], box[3] * scale[1]];
  const pad = 8;
  const sx = Math.max(0, x - pad);
  const sy = Math.max(0, y - pad);
  const sw = Math.min(img.naturalWidth - sx, w + 2 * pad);
  const sh = Math.min(img.naturalHeight - sy, h + 2 * pad);
  const fit = Math.min(canvas.width / sw, canvas.height / sh);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(img, sx, sy, sw, sh, (canvas.width - sw * fit) / 2, (canvas.height - sh * fit) / 2, sw * fit, sh * fit);
}

// In the all-fields list the agreeing fields get a quiet box on the page too
function matchBoxes() {
  for (const node of document.querySelectorAll(".cmp-box.match")) node.remove();
  const inner = document.querySelector(".cmp-page-inner");
  if (!inner || !c.all || !c.fields) return;
  for (const field of c.fields.list) {
    if (field.state === "mismatch") continue;
    const box = boxStyle(field.box);
    if (box) inner.append(el("div", { class: "cmp-box match", "data-name": field.name, style: box, title: `${field.name}: ${field.external || "(blank)"} = ${field.ours || "(blank)"}`, onclick: () => peek(field) }));
  }
  placeBoxes();
}

function peek(field) {
  const i = c.sheet.items.findIndex((item) => item.name === field.name);
  if (i >= 0) return go(i);
  c.peek = field;
  render();
  focusBox();
}

// Names to show: "q1..q50, roll" (ranges and names), or any text in the name
function filterMatch(name) {
  const text = c.filter.trim();
  if (!text) return true;
  for (const part of text.split(",").map((p) => p.trim()).filter(Boolean)) {
    try {
      if (part.includes("..") && expandRange(part).includes(name)) return true;
    } catch (error) {
      /* not a range */
    }
    if (name.toLowerCase().includes(part.toLowerCase())) return true;
  }
  return false;
}

const FIELD_STATES = { mismatch: "differs", match: "same", skipped: "rules skip it", outside: "outside the level" };

function renderAllList(list) {
  if (!c.fields || c.fields.scan_id !== c.sheet.scan_id) {
    list.append(el("div", { class: "muted small", style: { padding: "6px 8px" } }, "Loading every field…"));
    return;
  }
  const rows = c.fields.list.filter((f) => filterMatch(f.name));
  const counts = c.fields.list.reduce((acc, f) => ((acc[f.state] = (acc[f.state] || 0) + 1), acc), {});
  const current = c.sheet.items[c.idx];
  for (const field of rows) {
    const choice = choiceSource(c.choices[field.name]);
    const isCurrent = c.peek ? c.peek.name === field.name : current && current.name === field.name;
    list.append(
      el(
        "div",
        { class: `cmp-row ${field.state}${isCurrent ? " current" : ""}`, onclick: () => peek(field), title: FIELD_STATES[field.state] },
        el("code", {}, field.name),
        el("span", { class: choice === "external" ? "picked" : "" }, shown(field.external)),
        el("span", { class: choice === "ours" ? "picked" : "" }, shown(field.ours)),
        el("span", { class: choice === "manual" ? "picked" : "muted small" }, choice === "manual" ? shown(c.choices[field.name].value) : field.state === "mismatch" ? choice || "undecided" : FIELD_STATES[field.state])
      )
    );
  }
  list.prepend(el("div", { class: "cmp-row head muted small" }, el("span", {}, `${rows.length} of ${c.fields.list.length}`), el("span", {}, "theirs"), el("span", {}, "ours"), el("span", {}, `${counts.mismatch || 0} differ · ${counts.match || 0} same`)));
}

// O cycles the page shown; its ribbon button and the badge on the page say which one is on
function cycleView() {
  setView(VIEWS[(VIEWS.indexOf(c.view) + 1) % VIEWS.length]);
  toast(`Showing: ${VIEW_NAMES[c.view]}`, "", 1200);
}

function setView(view) {
  c.view = view;
  showView();
}

// The colour page shares the aligned page's coordinates; on the original
// scan each box follows the outline mapped back through the page transform
// (a box with no outline there, such as a joined value, is hidden)
async function showView() {
  const viewButton = document.getElementById("cmp-view");
  if (viewButton) viewButton.lastChild.textContent = VIEW_NAMES[c.view];
  const badge = document.getElementById("cmp-view-badge");
  if (badge) badge.textContent = VIEW_NAMES[c.view];
  const alt = document.querySelector(".cmp-alt");
  if (!alt || !c.sheet) return;
  const aligned = c.view === "aligned";
  const loaded = c.alt && c.alt.view === c.view && c.alt.scan_id === c.sheet.scan_id;
  c.image.hidden = !aligned && Boolean(loaded);
  alt.hidden = aligned || !loaded;
  placeBoxes();
  drawZoom();
  if (aligned || loaded) return focusBox(false);
  const scanId = c.sheet.scan_id;
  const view = c.view;
  try {
    const meta = await api(`/scans/${scanId}/views/${view}`);
    if (!c.sheet || c.sheet.scan_id !== scanId || c.view !== view) return;
    const img = new Image();
    img.src = url(meta.image_url);
    await img.decode();
    if (!c.sheet || c.sheet.scan_id !== scanId || c.view !== view) return;
    c.alt = { scan_id: scanId, view, img, meta };
    alt.src = img.src;
  } catch (error) {
    if (c.view === view) toast(`${VIEW_NAMES[view]}: ${error.message}`, "error", 5000);
    return;
  }
  showView();
}

function altFor(view) {
  return c.alt && c.sheet && c.alt.view === view && c.alt.scan_id === c.sheet.scan_id ? c.alt : null;
}

// [x, y, w, h] of a field on the original scan, from the mapped outline
function originalBox(name) {
  const alt = altFor("original");
  const map = alt && alt.meta.map;
  if (!map) return null;
  const quad = (map.fields[name] && map.fields[name].box) || map.zones[name];
  if (!quad) return null;
  const xs = quad.map((p) => p[0]);
  const ys = quad.map((p) => p[1]);
  return { box: [Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)], quad, size: [alt.meta.width, alt.meta.height] };
}

function placeBoxes() {
  if (!c.sheet) return;
  const original = c.view === "original" && altFor("original");
  for (const node of document.querySelectorAll(".cmp-box")) {
    const name = node.dataset.name;
    const item = node.dataset.i !== undefined ? c.sheet.items[Number(node.dataset.i)] : (c.fields && c.fields.list.find((f) => f.name === name));
    if (!item) continue;
    let style = null;
    if (!original) style = boxStyle(item.box);
    else {
      const mapped = originalBox(name);
      style = mapped ? boxStyle(mapped.box, mapped.size) : null;
    }
    node.hidden = !style || (c.view === "original" && !original);
    if (style) Object.assign(node.style, style);
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

function boxStyle(box, size = c.sheet.page_size) {
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
  for (const node of document.querySelectorAll(".cmp-box[data-i]")) {
    const i = Number(node.dataset.i);
    node.classList.toggle("current", !c.peek && i === c.idx);
    node.classList.toggle("decided", Boolean(c.choices[items[i].name]));
  }
  for (const node of document.querySelectorAll(".cmp-box.match")) node.classList.toggle("current", Boolean(c.peek && c.peek.name === node.dataset.name));
  const current = document.getElementById("cmp-current");
  current.innerHTML = "";
  if (c.peek) {
    current.append(
      el("div", { class: "row gap wrap" }, el("h3", {}, c.peek.name), el("span", { class: "muted small" }, FIELD_STATES[c.peek.state])),
      el("div", { class: "cmp-values" }, el("div", { class: "cmp-value" }, el("span", { class: "k" }, "External"), el("span", { class: "v" }, shown(c.peek.external))), el("div", { class: "cmp-value" }, el("span", { class: "k" }, "Ours"), el("span", { class: "v" }, shown(c.peek.ours)))),
      el("div", { class: "rv-meta" }, "The two readers agree here. S goes back to the mismatches; to change our value, open the sheet in Results (V).")
    );
  } else if (item) {
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
  list.classList.toggle("all", c.all);
  const filter = document.getElementById("cmp-filter");
  if (filter) filter.hidden = !c.all;
  if (c.all) renderAllList(list);
  else for (const [i, it] of items.entries()) {
    const choice = choiceSource(c.choices[it.name]);
    list.append(
      el(
        "div",
        { class: `cmp-row${i === c.idx ? " current" : ""}`, onclick: () => go(i) },
        el("code", {}, it.name),
        el("span", { class: choice === "external" ? "picked" : "" }, shown(it.external)),
        el("span", { class: choice === "ours" ? "picked" : "" }, shown(it.ours)),
        el("span", { class: choice === "manual" ? "picked" : "muted small" }, choice === "manual" ? shown(c.choices[it.name].value) : choice || "undecided")
      )
    );
  }
  // The current row stays in view in the list
  const row = list.querySelector(".cmp-row.current");
  if (row) {
    const head = list.querySelector(".cmp-row.head");
    const top = row.offsetTop - (head ? head.offsetHeight : 0);
    if (top < list.scrollTop || row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = Math.max(0, top - list.clientHeight / 3);
  }
  progressLine();
  drawZoom();
  renderPin();
}

// The close-up is cut from the page shown: aligned, colour or original
// (on the original only where the field has a mapped outline)
function zoomSource(item) {
  const size = c.sheet.page_size;
  if (c.view === "color" && altFor("color")) {
    const img = altFor("color").img;
    return { img, box: item.box, scale: [img.naturalWidth / size[0], img.naturalHeight / size[1]] };
  }
  if (c.view === "original") {
    const mapped = originalBox(item.name);
    if (mapped) {
      const img = altFor("original").img;
      return { img, box: mapped.box, quad: mapped.quad, scale: [img.naturalWidth / mapped.size[0], img.naturalHeight / mapped.size[1]] };
    }
  }
  const img = c.image;
  return { img, box: item.box, scale: [img.naturalWidth / size[0], img.naturalHeight / size[1]] };
}

// The close-up: the field alone, the field with its neighbours, or a wide
// slice of the page centred on the field. W steps through them; remembered
const ZOOMS = ["field", "medium", "wide"];
const ZOOM_NAMES = { field: "Field only", medium: "Nearby", wide: "Wide" };
const ZOOM_HEIGHT = { field: 150, medium: 205, wide: 260 };

function zoomMode() {
  const mode = safeStorage("get", "omr_cmp_zoom");
  return ZOOMS.includes(mode) ? mode : "wide";
}

function zoomButtonText() {
  return `Close-up: ${ZOOM_NAMES[zoomMode()].toLowerCase()}`;
}

// The page and the panel: how wide the panel is and how tall both are, set
// by dragging the divider and the grip under them; remembered
function layout() {
  try {
    return JSON.parse(safeStorage("get", "omr_cmp_layout") || "{}") || {};
  } catch {
    return {};
  }
}

function setLayout(change) {
  safeStorage("set", "omr_cmp_layout", JSON.stringify({ ...layout(), ...change }));
  applyLayout();
  drawZoom();
}

function applyLayout() {
  const review = document.getElementById("cmp-review");
  if (!review) return;
  const { side, height } = layout();
  review.style.setProperty("--cmp-side-w", side ? `${side}px` : "");
  review.style.setProperty("--cmp-h", height ? `${height}px` : "");
}

function drag(e, move, done) {
  e.preventDefault();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);
  handle.classList.add("on");
  const onMove = (ev) => move(ev);
  const onUp = (ev) => {
    handle.removeEventListener("pointermove", onMove);
    handle.removeEventListener("pointerup", onUp);
    handle.classList.remove("on");
    done(ev);
  };
  handle.addEventListener("pointermove", onMove);
  handle.addEventListener("pointerup", onUp);
}

function dragSplit(e) {
  const review = document.getElementById("cmp-review");
  const ribbon = review.querySelector(".cmp-ribbon");
  const width = (ev) => {
    const right = ribbon.getBoundingClientRect().left - 12;
    return Math.round(Math.min(Math.max(300, right - ev.clientX), review.clientWidth - 360));
  };
  drag(e, (ev) => review.style.setProperty("--cmp-side-w", `${width(ev)}px`), (ev) => setLayout({ side: width(ev) }));
}

function dragHeight(e) {
  const review = document.getElementById("cmp-review");
  const top = review.getBoundingClientRect().top;
  const height = (ev) => Math.round(Math.max(300, ev.clientY - top));
  drag(e, (ev) => review.style.setProperty("--cmp-h", `${height(ev)}px`), (ev) => setLayout({ height: height(ev) }));
}

function toggleWide() {
  const mode = ZOOMS[(ZOOMS.indexOf(zoomMode()) + 1) % ZOOMS.length];
  safeStorage("set", "omr_cmp_zoom", mode);
  const canvas = document.querySelector(".cmp-zoom");
  if (canvas) canvas.height = ZOOM_HEIGHT[mode];
  const button = document.getElementById("cmp-wide");
  if (button) button.lastChild.textContent = zoomButtonText();
  toast(`Close-up: ${ZOOM_NAMES[mode].toLowerCase()}`, "ok", 1200);
  drawZoom();
}

function drawZoom() {
  const canvas = document.querySelector(".cmp-zoom");
  const item = c.peek || (c.sheet && c.sheet.items[c.idx]);
  if (!canvas || !c.image || !item || !item.box || !c.sheet.page_size) return;
  const { img, box, quad, scale } = zoomSource(item);
  if (!img.naturalWidth) return;
  // Sharp at any panel width: as many canvas pixels as the screen shows
  const shown = canvas.clientWidth;
  if (shown) {
    const dpr = window.devicePixelRatio || 1;
    const width = Math.round(shown * dpr);
    // A wider panel shows more around the field, not a taller close-up
    const height = Math.round(ZOOM_HEIGHT[zoomMode()] * Math.min(1.25, shown / 420) * dpr);
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
  }
  const [x, y, w, h] = [box[0] * scale[0], box[1] * scale[1], box[2] * scale[0], box[3] * scale[1]];
  let sx, sy, sw, sh;
  const mode = zoomMode();
  if (mode !== "field") {
    // The canvas's shape, the field in the middle: about half the page wide
    // (wide) or a quarter of it (nearby)
    const [times, part] = mode === "wide" ? [2.5, 0.45] : [1.6, 0.26];
    sw = Math.min(img.naturalWidth, Math.max(w * times, img.naturalWidth * part));
    sh = Math.min(img.naturalHeight, (sw * canvas.height) / canvas.width);
    sx = Math.min(Math.max(0, x + w / 2 - sw / 2), img.naturalWidth - sw);
    sy = Math.min(Math.max(0, y + h / 2 - sh / 2), img.naturalHeight - sh);
  } else {
    const pad = Math.max(16, h * 0.8);
    sx = Math.max(0, x - pad);
    sy = Math.max(0, y - pad);
    sw = Math.min(img.naturalWidth - sx, w + 2 * pad);
    sh = Math.min(img.naturalHeight - sy, h + 2 * pad);
  }
  const fit = Math.min(canvas.width / sw, canvas.height / sh);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const dx = (canvas.width - sw * fit) / 2;
  const dy = (canvas.height - sh * fit) / 2;
  ctx.drawImage(img, sx, sy, sw, sh, dx, dy, sw * fit, sh * fit);
  ctx.strokeStyle = "#ff8c00";
  ctx.lineWidth = 2 * (window.devicePixelRatio || 1);
  if (quad) {
    ctx.beginPath();
    quad.forEach(([px, py], i) => ctx[i ? "lineTo" : "moveTo"](dx + (px * scale[0] - sx) * fit, dy + (py * scale[1] - sy) * fit));
    ctx.closePath();
    ctx.stroke();
  } else ctx.strokeRect(dx + (x - sx) * fit - 3, dy + (y - sy) * fit - 3, w * fit + 6, h * fit + 6);
  const label = document.getElementById("cmp-zoom-view");
  if (label) label.textContent = img === c.image && c.view !== "aligned" ? `Close-up: aligned page (${c.view === "original" ? "no outline on the original for this field" : "loading colour…"})` : `Close-up: ${VIEW_NAMES[c.view].toLowerCase()}`;
}

// The page scrolls so the current field sits in the middle of it: on a new
// sheet at once, on the next field smoothly
function focusBox(smooth = true) {
  const node = c.peek ? document.querySelector(`.cmp-box.match[data-name="${CSS.escape(c.peek.name)}"]`) : document.querySelector(`.cmp-box[data-i="${c.idx}"]`);
  const page = document.querySelector(".cmp-page");
  if (!node || node.hidden || !page) return;
  const outer = page.getBoundingClientRect();
  const inner = node.getBoundingClientRect();
  const top = page.scrollTop + inner.top - outer.top - (page.clientHeight - inner.height) / 2;
  const left = page.scrollLeft + inner.left - outer.left - (page.clientWidth - inner.width) / 2;
  page.scrollTo({ top: Math.max(0, top), left: Math.max(0, left), behavior: smooth ? "smooth" : "auto" });
}

function go(i) {
  if (!c.sheet || !c.sheet.items.length) return;
  const n = c.sheet.items.length;
  c.peek = null;
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

// The top row: where you are and what is left, as a few labelled numbers;
// the rest of the comparison's counts on a quieter line below them
function progressLine() {
  const node = document.getElementById("cmp-progress");
  if (!node) return;
  node.innerHTML = "";
  const p = c.session && c.session.progress;
  if (!p) return;
  const b = c.session.building;
  const stat = (label, value, title, extra) => el("span", { class: "cmp-stat", title }, el("span", { class: "k" }, label), el("span", { class: "v" }, value), extra || null);
  const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
  const stats = [];
  if (b) stats.push(stat("Comparing", b.total ? `${b.done.toLocaleString()} of ${b.total.toLocaleString()}` : "…", "The comparison is still running: sheets with a mismatch show up as they are found"));
  if (c.sheet) stats.push(stat("Sheet", `${c.sheet.position} of ${p.to_review.toLocaleString()}${b ? "+" : ""}`, "Sheets with a mismatch, in order: where this one is"));
  stats.push(stat("Sheets left", p.open.toLocaleString(), "Sheets with a mismatch not saved yet (this one included)"));
  const pct = p.mismatches ? Math.round((100 * p.decided) / p.mismatches) : 100;
  stats.push(stat("Decided", `${p.decided.toLocaleString()} of ${p.mismatches.toLocaleString()} mismatches`, "Fields where the two readers differ, and how many of them have a choice saved", el("span", { class: "cmp-bar" }, el("span", { style: { width: `${pct}%` } }))));
  const notes = [];
  const states = p.sheets || {};
  if (states.clean) notes.push(el("span", { title: "Sheets where both readers agree on every compared field: nothing to do" }, `${plural(states.clean, "sheet")} agree`));
  if (p.chose_external) notes.push(el("span", { title: "Choices that kept the other reader's value" }, `their value kept ${p.chose_external.toLocaleString()}×`));
  const missing = states.no_record || 0;
  if (p.unmatched_external || missing) notes.push(el("span", { title: "Rows of their file with no sheet of ours, and sheets of ours with no row in their file. Check the matching under Rules…" }, `not matched: ${plural(p.unmatched_external, "row")} of theirs, ${plural(missing, "sheet")} of ours`));
  if (states.replaced) notes.push(el("span", { title: "A file read again: only its newest scan is compared" }, `${plural(states.replaced, "older scan")} of a file read again`));
  if (states.duplicate) notes.push(el("span", { title: "The same file in several scans, set not to be compared" }, `${plural(states.duplicate, "sheet")} not compared (same file twice)`));
  if (states.not_read) notes.push(el("span", { title: "Their row matched a sheet we could not read" }, `${plural(states.not_read, "sheet")} we could not read`));
  node.append(...stats);
  if (notes.length) node.append(el("span", { class: "cmp-notes muted small" }, notes.flatMap((n, i) => (i ? [" · ", n] : [n]))));
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
    // Switch tabs after this key press: on Results, V would verify the sheet
    e.preventDefault();
    e.stopImmediatePropagation();
    setTimeout(openInResults, 0);
  } else if (key === "a" || key === "A") {
    e.preventDefault();
    toggleAll();
  } else if (key === "w" || key === "W") {
    e.preventDefault();
    toggleWide();
  } else if (key === "g" || key === "G") {
    e.preventDefault();
    document.getElementById("cmp-goto-q").focus();
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
  // Every job of the template by default: the newest job can be a small
  // test run that holds none of their rows
  const fillJobs = async () => {
    job.innerHTML = "";
    job.append(el("option", { value: "" }, everyJob));
    try {
      const jobs = (await api("/jobs?limit=200")).jobs.filter((j) => !j.template_id || j.template_id === template.value);
      jobs.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
      for (const j of jobs) {
        job.append(el("option", { value: j.id }, `${j.name || j.id.slice(0, 8)} · ${j.total_files || j.processed_files || 0} sheets · ${j.created_at ? new Date(j.created_at * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : ""}`));
      }
    } catch (error) {
      /* jobs are optional */
    }
  };
  template.addEventListener("change", () => {
    fillJobs();
    if (preview) readFile();
  });
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
  const chosenProfile = () => profiles.find((p) => p.name === profile.value);
  const downloadProfile = () => {
    const chosen = chosenProfile();
    if (!chosen) return toast("Choose a saved layout first");
    saveProfileFile("compare", { name: chosen.name, template_id: chosen.template_id || null, settings: chosen.settings });
  };
  const uploadProfiles = async () => {
    const found = await pickProfileFiles("compare");
    if (!found.length) return;
    const clashes = found.filter((p) => profiles.some((q) => q.name === p.name)).map((p) => p.name);
    if (clashes.length && !confirm(`Replace the saved layout${clashes.length > 1 ? "s" : ""} ${clashes.join(", ")}?`)) return;
    let saved = 0;
    for (const item of found) {
      const settings = item.settings || {};
      try {
        await api("/compare/profiles", { method: "POST", json: { name: item.name, template_id: item.template_id, parse: settings.parse || {}, mapping: settings.mapping || {}, key: settings.key || {}, rules: settings.rules || {} } });
        saved += 1;
      } catch (error) {
        toast(`${item.name}: ${error.message}`, "error", 7000);
      }
    }
    await loadProfiles();
    if (!saved) return;
    profile.value = found[found.length - 1].name;
    profile.dispatchEvent(new Event("change"));
    const missing = found.filter((p) => p.template_id && !state.templates.some((t) => t.id === p.template_id));
    toast(saved === 1 ? `Added layout "${profile.value}"` : `Added ${saved} layouts`, "ok");
    if (missing.length) toast(`Made for template ${missing[0].template_id}, which is not on this computer: pick the template yourself`, "", 7000);
  };
  const deleteProfile = async () => {
    const chosen = chosenProfile();
    if (!chosen) return toast("Choose a saved layout first");
    if (!confirm(`Delete the saved layout "${chosen.name}"?`)) return;
    try {
      await api(`/compare/profiles/${encodeURIComponent(chosen.name)}`, { method: "DELETE" });
      profile.value = "";
      await loadProfiles();
      toast("Layout deleted", "ok");
    } catch (error) {
      toast(error.message, "error");
    }
  };

  const file = el("input", { type: "file", accept: ".csv,.txt,.dat,.tsv,.prn" });
  const format = el("select", {}, el("option", { value: "auto" }, "Auto"), el("option", { value: "csv" }, "CSV / delimited"), el("option", { value: "fixed" }, "Fixed width"));
  const delimiter = el("select", {}, el("option", { value: "" }, "auto"), el("option", { value: "," }, ", comma"), el("option", { value: ";" }, "; semicolon"), el("option", { value: "tab" }, "tab"), el("option", { value: "|" }, "| pipe"));
  const header = el("input", { type: "number", min: 0, value: 1, style: { width: "4.5em" }, title: "The line with the column names: 1 = the first line, 0 = the file has no header" });
  const names = el("input", { size: 40, placeholder: "e.g. SEQ, ROLL, q1..q200 (leave empty to use the header)", spellcheck: "false" });
  const fixed = el("textarea", { rows: 4, cols: 40, placeholder: "One column per line: NAME START LENGTH\nROLL 1 12\nq1..q200 13 1", spellcheck: "false" });
  const split = el("textarea", { rows: 3, cols: 40, placeholder: "One packed column per line: COLUMN FIELDS [width=N] [sep=X]\nANSWERS q1..q200\nA1 q1..q10\nA2 q11..q20", spellcheck: "false" });
  const fixedRow = el("label", { class: "field" }, "Fixed-width columns (START counts from 1)", fixed);
  // The file is read as soon as it is picked; these settings are only for a
  // file the automatic reading gets wrong (no header, fixed width, packed answers)
  const howRead = el("details", { class: "cmp-how" }, el("summary", {}, "How the file is read (change only if the columns below look wrong)"));
  file.addEventListener("change", () => file.files.length && readFile());
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
    if (saved.format && saved.format !== "auto") howRead.open = true;
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
    if (chosen.template_id && chosen.template_id !== template.value && state.templates.some((t) => t.id === chosen.template_id)) {
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
    // One column usually means the delimiter or the layout was not recognised
    if (preview.columns.length < 2) howRead.open = true;
    const chosen = profiles.find((p) => p.name === profile.value);
    const settings = chosen ? chosen.settings : {};
    // Keep what was typed in the mapping for columns that are still there
    const mapping = chosen ? { ...(settings.mapping || {}) } : { ...preview.mapping };
    if (edited) for (const [column, value] of Object.entries(edited)) if (preview.columns.includes(column)) mapping[column] = value;
    parts = showPreview(step2, { ...preview, mapping }, settings);
    parts.key.node.addEventListener("change", checkMatch);
    checkMatch();
  };

  // Step 3's check: how many of their rows find one of our sheets
  const matchBox = el("div", { class: "cmp-match" });
  let matchRun = 0;
  const checkMatch = async () => {
    if (!preview || !parts) return;
    const run = ++matchRun;
    matchBox.className = "cmp-match";
    matchBox.textContent = "Checking which of their rows find our sheets…";
    let m;
    try {
      m = await api(`/compare/preview/${preview.upload}/match`, { method: "POST", json: { template_id: template.value, job_id: job.value || null, parse: parse(), mapping: parts.mapping(), key: parts.key.read() } });
    } catch (error) {
      if (run === matchRun) matchBox.textContent = error.message;
      return;
    }
    if (run !== matchRun) return;
    // A job that holds none of their rows while others do: take every job
    if (!m.matched && job.value && (m.other_jobs || []).length) {
      const from = job.options[job.selectedIndex].textContent.split(" · ")[0];
      job.value = "";
      await checkMatch();
      matchBox.prepend(el("div", { class: "small" }, `The job ${from} holds none of their rows, so every job of the template is used instead.`));
      return;
    }
    showMatch(matchBox, m, { job, onJob: (id) => { job.value = id; checkMatch(); } });
  };
  job.addEventListener("change", checkMatch);

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

  for (const node of [el("div", { class: "row gap wrap" }, el("label", { class: "field inline" }, "Format ", format), el("label", { class: "field inline" }, "Delimiter ", delimiter), el("label", { class: "field inline" }, "Header row ", header)), el("label", { class: "field inline", title: "Name the columns yourself: replaces the header, or names a file without one (ranges like q1..q200 allowed)" }, "Column names ", names), fixedRow, el("label", { class: "field", title: "A column that holds many answers: split it into one column per field" }, "Packed answer columns (optional)", split), el("div", { class: "row gap" }, el("button", { class: "small", onclick: readFile }, "Read again with these settings"))]) howRead.append(node);
  const step = (n, title, hint, ...content) => el("section", { class: "cmp-step" }, el("h4", {}, el("span", { class: "n" }, String(n)), title), hint ? el("p", { class: "muted small" }, hint) : null, ...content);
  step2.append(el("p", { class: "muted small cmp-wait" }, "Pick their file above: the next steps appear once it is read."));
  const body = el(
    "div",
    { class: "cmp-form cmp-new" },
    step(1, "Our sheets", "Which of our results their file is checked against.", el("div", { class: "row gap wrap" }, el("label", { class: "field inline" }, "Template ", template), el("label", { class: "field inline" }, "Our sheets ", job))),
    step(
      2,
      "Their file",
      "The other scanner's export: OpScan .dat, CSV or fixed width. It is read as soon as you pick it. A saved layout fills in every setting from last time.",
      el("div", { class: "row gap wrap" }, el("label", { class: "field inline" }, "File ", file), el("label", { class: "field inline", title: "A layout saved earlier: how a file is read, matched and compared" }, "Saved layout ", profile), el("button", { class: "small", title: "Save the chosen layout as a file, to use it on another computer", onclick: downloadProfile }, "Download"), el("button", { class: "small", title: "Add layouts from files downloaded on another computer", onclick: uploadProfiles }, "Upload…"), el("button", { class: "small danger ghost", title: "Delete the chosen saved layout", onclick: deleteProfile }, "Delete")),
      howRead
    ),
    step2,
    el("label", { class: "field inline cmp-name" }, "Name this comparison ", name)
  );
  // Their sample, the matching and the columns go into steps 3 to 5
  step2.matchBox = matchBox;
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
  const step = (n, title, hint, ...content) => el("section", { class: "cmp-step" }, el("h4", {}, el("span", { class: "n" }, String(n)), title), hint ? el("p", { class: "muted small" }, hint) : null, ...content);
  container.append(
    el("div", { class: "rv-meta" }, `Read ${preview.count.toLocaleString()} rows and ${preview.columns.length} columns from ${preview.file_name}. The first rows (␣ = a space):`),
    el("div", { class: "cmp-sample" }, table),
    step(3, "Match their rows to our sheets", "Each row of their file is paired with one of our sheets by a value both sides have, usually the sheet number in their file and our file name.", key.node, container.matchBox || null),
    step(
      4,
      "Columns to compare",
      null,
      el(
        "details",
        { open: !mapped },
        el("summary", {}, mapped ? `${mapped} of their ${preview.columns.length} columns are matched to our fields. Open to check or change.` : "None of their columns matched our field names: map them here."),
        el("p", { class: "muted small" }, "Type our field next to each of their columns; clear a box to leave that column out."),
        el("div", { class: "row gap wrap" }, el("span", { class: "small" }, "Map from column"), from, el("span", { class: "small" }, "onwards to"), range, el("button", { class: "small", onclick: fill }, "Fill"), el("button", { class: "small ghost", onclick: clear }, "Clear all")),
        list,
        mapping
      )
    ),
    step(5, "Rules", null, el("details", {}, el("summary", {}, "How values are compared (the defaults suit most files)"), rules.node))
  );
  return {
    key,
    rules,
    mapping: () => Object.fromEntries(Object.entries(inputs).map(([column, input]) => [column, input.value.trim() || null])),
  };
}

// The result of the match check, in words, with the jobs that hold the
// sheets their unmatched rows need
function showMatch(box, m, { job, onJob }) {
  box.innerHTML = "";
  const all = m.rows && m.matched === m.rows;
  box.className = `cmp-match ${all ? "ok" : m.matched ? "warn" : "bad"}`;
  box.append(el("div", { class: "cmp-match-line" }, el("strong", {}, `${m.matched.toLocaleString()} of ${m.rows.toLocaleString()} rows`), ` of their file find one of our ${m.sheets.toLocaleString()} sheets.`));
  if (m.their_unmatched) {
    box.append(
      el(
        "div",
        { class: "small" },
        `${m.their_unmatched.toLocaleString()} of their rows have no sheet here, for example `,
        m.their_examples.map((x, i) => [i ? ", " : "", el("code", {}, x.key || "(empty)"), el("span", { class: "muted" }, ` (row ${x.row}${x.reason ? `, ${x.reason}` : ""})`)]),
        "."
      )
    );
  }
  if (m.our_unmatched) {
    box.append(el("div", { class: "small" }, `${m.our_unmatched.toLocaleString()} of our sheets have no row in their file, for example `, m.our_examples.map((x, i) => [i ? ", " : "", el("code", {}, x.file || x.key)]), "."));
  }
  for (const other of m.other_jobs || []) {
    const option = [...job.options].find((o) => o.value === other.job_id);
    const label = option ? option.textContent.split(" · ")[0] : other.job_id.slice(0, 8);
    box.append(el("div", { class: "row gap wrap small" }, `The job ${label} holds sheets for ${other.rows.toLocaleString()} of those rows.`, el("button", { class: "small", onclick: () => onJob("") }, "Use every job of the template"), option ? el("button", { class: "small ghost", onclick: () => onJob(other.job_id) }, `Use ${label} instead`) : null));
  }
  // Far apart key ranges say "wrong file or wrong sheets" at a glance
  if (m.their_range && m.our_range && m.matched < m.rows) box.append(el("div", { class: "small" }, `Their keys run ${m.their_range[0]}–${m.their_range[1]}; these sheets run ${m.our_range[0]}–${m.our_range[1]}.`));
  if (!m.matched && m.rows) box.append(el("div", { class: "small" }, "Nothing matches: check the column and what it matches above, pick other sheets in step 1, or check that this is the right file."));
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
