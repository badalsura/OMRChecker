// Compare tab: reconcile our results with another reader's export (a physical
// OMR scanner such as Scantron OpScan 6, or any CSV / fixed-width file).
// Mismatches only, one sheet at a time, keyboard first:
//   1 external value · 2 our value · R undo / previous field · S next mismatch
//   Tab skip sheet · Shift+Tab previous sheet · Enter save and next
// Choices go to the reconciled layer on the server; read results never change.
import { api, chip, confidenceColor, displayName, el, modal, on, safeStorage, saveAs, state, toast, url } from "./api.js";

const c = {
  sessions: [],
  session: null,
  sheet: null,
  image: null,
  idx: 0,
  choices: {},
  history: [],
  busy: false,
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
  await claim({});
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
      return showEmpty(
        left ? `The ${left} open sheet${left === 1 ? " is" : "s are"} being worked on by someone else.` : summary.mismatches ? "Every mismatch is reconciled. Export the reconciled data." : "No mismatches: the two readers agree on every compared field.",
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

function changedDecisions() {
  const out = {};
  for (const item of c.sheet ? c.sheet.items : []) {
    const choice = c.choices[item.name] || null;
    if (choice !== (item.source || null)) out[item.name] = choice;
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
    if (data.state !== "done") toast(`Saved; ${c.sheet.items.filter((i) => !c.choices[i.name]).length} mismatch(es) on this sheet left open`, "", 2500);
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
  c.choices = Object.fromEntries(sheet.items.map((item) => [item.name, item.source || null]));
  c.history = [];
  const firstOpen = sheet.items.findIndex((item) => !item.source);
  c.idx = firstOpen >= 0 ? firstOpen : 0;
  const main = document.getElementById("cmp-main");
  main.innerHTML = "";
  const img = el("img", { class: "cmp-img", alt: sheet.file_name || "sheet", src: url(sheet.page) });
  c.image = img;
  const inner = el("div", { class: "cmp-page-inner" }, img);
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
        sheet.state === "done" ? el("div", { class: "rv-meta" }, `Reconciled by ${sheet.done_by || "someone"}: saving changes it.`) : null,
        el("canvas", { class: "cmp-zoom", width: 420, height: 150 }),
        el("div", { id: "cmp-current" }),
        el("div", { class: "cmp-list", id: "cmp-list" }),
        el(
          "div",
          { class: "cmp-actions" },
          el("button", { onclick: () => choose("external"), title: "Keep the other reader's value (1)" }, el("kbd", {}, "1"), " External"),
          el("button", { onclick: () => choose("ours"), title: "Keep our value (2)" }, el("kbd", {}, "2"), " Ours"),
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
  render();
  main.focus({ preventScroll: true });
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
    const choice = c.choices[item.name];
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
      )
    );
  }
  const list = document.getElementById("cmp-list");
  list.innerHTML = "";
  for (const [i, it] of items.entries()) {
    const choice = c.choices[it.name];
    list.append(
      el(
        "div",
        { class: `cmp-row${i === c.idx ? " current" : ""}`, onclick: () => go(i) },
        el("code", {}, it.name),
        el("span", { class: choice === "external" ? "picked" : "" }, shown(it.external)),
        el("span", { class: choice === "ours" ? "picked" : "" }, shown(it.ours)),
        el("span", { class: "muted small" }, choice ? (choice === "external" ? "external" : "ours") : "open")
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
  if (c.sheet) parts.push(`Sheet ${c.sheet.position} of ${p.to_review}`);
  parts.push(`${p.open} open`, `decided ${p.decided} of ${p.mismatches} mismatches`);
  if (p.chose_external) parts.push(`${p.chose_external} external kept`);
  const summary = c.session.summary || {};
  const missing = (summary.states || {}).no_record || 0;
  if (p.unmatched_external || missing) parts.push(`unmatched: ${p.unmatched_external} of theirs, ${missing} of ours`);
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
  } else if (key === "r" || key === "R") {
    e.preventDefault();
    undo();
  } else if (key === "s" || key === "S") {
    e.preventDefault();
    go(c.idx + 1);
  }
}

// ---------------------------------------------------------------------------
// session actions
// ---------------------------------------------------------------------------
async function rebuild() {
  if (!c.session) return;
  try {
    c.session = await api(`/compare/sessions/${c.session.id}/rebuild`, { method: "POST" });
    toast(`Compared again: ${c.session.summary.mismatches} mismatches`, "ok");
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
  };
  f.fields.value = rules.fields || "all";
  f.blank.value = rules.blank || "mismatch";
  f.multi.value = rules.multi || "same";
  const node = el(
    "div",
    { class: "cmp-form" },
    el("label", { class: "field inline" }, "Compare ", f.fields, " ", f.list),
    el("label", { class: "field inline" }, "One side blank, the other marked ", f.blank),
    el("label", { class: "field inline" }, "Multi-marks ", f.multi),
    el("label", { class: "field inline", title: "What their file writes for an unmarked answer; 'space' is a single space" }, "Their blank is written as ", f.blank_codes),
    el("label", { class: "field inline", title: "What their file writes when more than one option is marked" }, "Their multi-mark is written as ", f.multi_codes),
    el("label", { class: "field inline" }, f.case, " Upper and lower case differ"),
    el("label", { class: "field inline", title: "A roll number with a blank digit: '12 45' equals '1245'" }, f.spaces, " Ignore spaces inside values")
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
  const job = el("select", {}, el("option", { value: "" }, "All sheets of the template"));
  const fillJobs = async () => {
    job.innerHTML = "";
    job.append(el("option", { value: "" }, "All sheets of the template"));
    try {
      for (const j of (await api("/jobs?limit=200")).jobs) {
        if (j.template_id && j.template_id !== template.value) continue;
        job.append(el("option", { value: j.id }, `${j.name || j.id.slice(0, 8)} · ${j.created_at ? new Date(j.created_at * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : ""}`));
      }
    } catch (error) {
      /* jobs are optional */
    }
  };
  template.addEventListener("change", fillJobs);
  fillJobs();
  const file = el("input", { type: "file", accept: ".csv,.txt,.dat,.tsv,.prn" });
  const format = el("select", {}, el("option", { value: "auto" }, "Auto"), el("option", { value: "csv" }, "CSV / delimited"), el("option", { value: "fixed" }, "Fixed width"));
  const delimiter = el("select", {}, el("option", { value: "" }, "auto"), el("option", { value: "," }, ", comma"), el("option", { value: ";" }, "; semicolon"), el("option", { value: "tab" }, "tab"), el("option", { value: "|" }, "| pipe"));
  const header = el("input", { type: "checkbox", checked: true });
  const fixed = el("textarea", { rows: 4, cols: 40, placeholder: "One column per line: NAME START LENGTH\nSEQ 1 10\nq1..q200 41 1", spellcheck: "false" });
  const fixedRow = el("label", { class: "field" }, "Fixed-width columns (START counts from 1)", fixed);
  const syncFormat = () => (fixedRow.style.display = format.value === "fixed" ? "" : "none");
  format.addEventListener("change", syncFormat);
  syncFormat();
  const step2 = el("div", {});
  const name = el("input", { placeholder: "e.g. OpScan batch 3", size: 30 });
  let preview = null;
  let parts = null;

  const parse = () => ({ format: format.value, delimiter: delimiter.value || null, header: header.checked, fixed: format.value === "fixed" ? fixed.value : "" });
  const readFile = async () => {
    if (!file.files.length) return toast("Pick their file first");
    if (!template.value) return toast("Pick a template");
    const form = new FormData();
    form.append("file", file.files[0]);
    form.append("template_id", template.value);
    form.append("parse", JSON.stringify(parse()));
    try {
      preview = await api("/compare/preview", { method: "POST", form });
    } catch (error) {
      return toast(error.message, "error", 6000);
    }
    if (!name.value) name.value = file.files[0].name.replace(/\.[^.]+$/, "");
    parts = showPreview(step2, preview);
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
      toast(`${session.summary.matched} sheets matched · ${session.summary.mismatches} mismatches`, "ok", 5000);
      await loadSessions(true);
    } catch (error) {
      toast(error.message, "error", 8000);
    }
  };

  const body = el(
    "div",
    { class: "cmp-form" },
    el("div", { class: "row gap wrap" }, el("label", { class: "field inline" }, "Template ", template), el("label", { class: "field inline" }, "Our sheets ", job)),
    el("div", { class: "row gap wrap" }, el("label", { class: "field inline" }, "Their file ", file), el("label", { class: "field inline" }, "Format ", format), el("label", { class: "field inline" }, "Delimiter ", delimiter), el("label", { class: "field inline" }, header, " First row is a header"), el("button", { onclick: readFile }, "Read file")),
    fixedRow,
    step2,
    el("label", { class: "field inline" }, "Name ", name)
  );
  const dialog = modal("New comparison", body, [el("button", { onclick: () => dialog.close() }, "Cancel"), el("button", { class: "primary", onclick: create }, "Compare")], { wide: true });
}

function showPreview(container, preview) {
  container.innerHTML = "";
  const table = el(
    "table",
    { class: "table small" },
    el("thead", {}, el("tr", {}, preview.columns.map((name) => el("th", {}, name)))),
    el("tbody", {}, preview.rows.map((row) => el("tr", {}, row.map((cell) => el("td", {}, cell === " " ? "␣" : cell)))))
  );
  const list = el("datalist", { id: "cmp-names" }, preview.names.concat(preview.questions.filter((q) => !preview.names.includes(q))).map((n) => el("option", { value: n })));
  const inputs = {};
  const mapping = el("div", { class: "cmp-mapping" });
  for (const column of preview.columns) {
    inputs[column] = el("input", { list: "cmp-names", value: preview.mapping[column] || "", placeholder: "not compared", size: 12 });
    mapping.append(el("label", {}, el("code", {}, column), inputs[column]));
  }
  const key = keyForm(preview, preview.key);
  const rules = rulesForm(preview.rules);
  const mapped = Object.keys(preview.mapping).length;
  container.append(
    el("div", { class: "rv-meta" }, `${preview.count} rows in ${preview.file_name}. First rows (␣ = a space):`),
    el("div", { class: "cmp-sample" }, table),
    el("h4", {}, "How their rows find our sheets"),
    key.node,
    el("h4", {}, `Columns to compare (${mapped} matched by name; clear a box to leave a column out)`),
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
      toast(`Compared again: ${c.session.summary.mismatches} mismatches; choices whose two values did not change are kept`, "ok", 5000);
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
