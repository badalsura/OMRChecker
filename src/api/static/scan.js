// Scan tab: drop sheets, read them synchronously, inspect results.
import { api, chip, confidenceColor, csvCell, download, el, emit, toast, url } from "./api.js";
import { appendPdfParams, mountPdfOptions } from "./pdf_options.js";

const CONCURRENCY = 3;
const sheets = []; // {id, file, name, state, result, error}
let selected = null;
let running = 0;
let counter = 0;

export function initScan() {
  mountPdfOptions("scan-pdf", "scan");
  const drop = document.getElementById("scan-drop");
  const input = document.getElementById("scan-files");
  drop.addEventListener("click", () => input.click());
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") input.click();
  });
  input.addEventListener("change", () => {
    addFiles([...input.files]);
    input.value = "";
  });
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    addFiles([...e.dataTransfer.files]);
  });
  document.getElementById("scan-camera").addEventListener("click", () => {
    const templateId = document.getElementById("scan-template").value;
    if (!templateId) {
      toast("Pick a template first", "error");
      return;
    }
    const query = new URLSearchParams({ source: "server", apiBase: location.origin, templateId });
    window.open(`/browser/demo.html?${query}`, "_blank");
  });
  document.getElementById("scan-clear").addEventListener("click", () => {
    sheets.splice(0, sheets.length, ...sheets.filter((s) => s.state === "queued" || s.state === "reading"));
    selected = null;
    renderList();
    renderDetail();
  });
  document.getElementById("scan-export").addEventListener("click", exportCsv);
  document.addEventListener("keydown", (e) => {
    if (!document.getElementById("tab-scan").classList.contains("active")) return;
    if (e.target.matches("input, select, textarea")) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const done = sheets;
      const index = done.findIndex((s) => s === selected);
      const next = done[index + (e.key === "ArrowDown" ? 1 : -1)];
      if (next) {
        selected = next;
        renderList();
        renderDetail();
        e.preventDefault();
      }
    }
  });
  renderStats();
}

function addFiles(files) {
  const templateId = document.getElementById("scan-template").value;
  if (!templateId) {
    toast("Pick a template first", "error");
    return;
  }
  for (const file of files) {
    sheets.unshift({ id: ++counter, file, name: file.name, state: "queued", templateId });
  }
  renderList();
  pump();
}

function pump() {
  while (running < CONCURRENCY) {
    const next = [...sheets].reverse().find((s) => s.state === "queued");
    if (!next) break;
    running++;
    next.state = "reading";
    renderList();
    readSheet(next).finally(() => {
      running--;
      renderList();
      pump();
    });
  }
}

async function readSheet(sheet) {
  const form = new FormData();
  form.append("template_id", sheet.templateId);
  form.append("files", sheet.file, sheet.name);
  appendPdfParams(form, "scan");
  const started = performance.now();
  try {
    const data = await api("/scans", { method: "POST", form });
    const results = data.scans;
    sheet.ms = Math.round(performance.now() - started);
    // Multi-page PDFs produce one result per page
    sheet.result = results[0];
    sheet.state = results[0].status;
    for (const extra of results.slice(1)) {
      sheets.unshift({ id: ++counter, name: extra.file_id, state: extra.status, result: extra, templateId: sheet.templateId });
    }
    if (!selected) selected = sheet;
    if (selected === sheet) renderDetail();
    emit("review-changed");
  } catch (error) {
    sheet.state = "error";
    sheet.error = error.message;
  }
  sheet.file = null;
}

function renderStats() {
  const counts = {};
  for (const s of sheets) counts[s.state] = (counts[s.state] || 0) + 1;
  const box = document.getElementById("scan-stats");
  box.innerHTML = "";
  box.append(el("span", { class: "chip" }, `${sheets.length} sheets`));
  for (const [state, n] of Object.entries(counts)) box.append(chip(`${n} ${state.replace("_", " ")}`, state));
}

function renderList() {
  const list = document.getElementById("scan-list");
  list.innerHTML = "";
  for (const sheet of sheets) {
    const score = sheet.result && sheet.result.score !== null && sheet.result.score !== undefined ? sheet.result.score : null;
    list.append(
      el(
        "li",
        {
          class: sheet === selected ? "selected" : "",
          onclick: () => {
            selected = sheet;
            renderList();
            renderDetail();
          },
        },
        chip(sheet.state, sheet.state),
        el("span", { class: "name", title: sheet.name }, sheet.name),
        score !== null ? el("span", { class: "muted small" }, `score ${score}`) : null
      )
    );
  }
  renderStats();
}

function renderDetail() {
  const box = document.getElementById("scan-detail");
  box.innerHTML = "";
  if (!selected) {
    box.append(el("div", { class: "empty-state" }, "Select a sheet to see its results."));
    return;
  }
  const sheet = selected;
  if (!sheet.result) {
    box.append(el("div", { class: "empty-state" }, sheet.error ? `Error: ${sheet.error}` : `${sheet.state}…`));
    return;
  }
  const r = sheet.result;
  const header = el(
    "div",
    { class: "row gap wrap", style: { marginBottom: "10px" } },
    el("h2", { style: { margin: 0 } }, r.file_id),
    chip(r.status, r.status),
    r.score !== null && r.score !== undefined ? el("span", { class: "chip" }, `score ${r.score}`) : null,
    el("span", { class: "muted small" }, `${r.timings_ms?.total ?? "?"} ms on server`),
    el("span", { class: "spacer" }),
    r.review && r.review.length
      ? el("button", { class: "small", onclick: () => emit("review-scan", r.scan_id) }, `Review ${r.review.length} item(s)`)
      : null,
    el("button", { class: "small", onclick: () => emit("results-scan", r.scan_id) }, "Open in Results"),
    el("a", { class: "button small ghost", href: url(`/scans/${r.scan_id}`), target: "_blank" }, "JSON")
  );
  box.append(header);
  if (r.error) box.append(el("div", { class: "chip error" }, r.error));

  const image = r.links && r.links.marked ? el("img", { src: url(r.links.marked), alt: "marked sheet" }) : el("div", { class: "empty-state" }, "No image stored");
  const rows = [];
  for (const [name, z] of Object.entries(r.zones || {})) {
    rows.push(resultRow(name, z.type, z.value, z.confidence, z.flags, z.needs_review));
  }
  const fieldNames = Object.keys(r.fields || {}).sort(naturalCompare);
  for (const name of fieldNames) {
    const f = r.fields[name];
    rows.push(resultRow(name, "bubbles", f.value, f.confidence, f.flags, f.needs_review));
  }
  const table = el(
    "table",
    { class: "table" },
    el("thead", {}, el("tr", {}, el("th", {}, "Field"), el("th", {}, "Type"), el("th", {}, "Value"), el("th", {}, "Confidence"), el("th", {}, "Flags"))),
    el("tbody", {}, rows)
  );
  box.append(el("div", { class: "detail-grid" }, el("div", {}, image), el("div", { class: "results-scroll" }, table)));
}

function resultRow(name, type, value, confidence, flags, needsReview) {
  return el(
    "tr",
    { class: needsReview ? "flagged" : "" },
    el("td", { class: "mono" }, name),
    el("td", { class: "muted small" }, type),
    el("td", { class: "mono" }, value === "" ? el("span", { class: "muted" }, "(blank)") : value),
    el("td", {}, el("span", { class: "conf", style: { background: confidenceColor(confidence) } }, confidence === null || confidence === undefined ? "–" : Number(confidence).toFixed(2))),
    el("td", {}, (flags || []).map((f) => chip(f, "flag")))
  );
}

export function naturalCompare(a, b) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

function exportCsv() {
  const done = sheets.filter((s) => s.result);
  if (!done.length) {
    toast("Nothing to export yet");
    return;
  }
  const columns = new Set();
  for (const s of done) Object.keys(s.result.responses || {}).forEach((c) => columns.add(c));
  const cols = [...columns].sort(naturalCompare);
  const lines = [["file", "scan_id", "status", "score", "needs_review", ...cols].map(csvCell).join(",")];
  for (const s of [...done].reverse()) {
    const r = s.result;
    lines.push(
      [r.file_id, r.scan_id, r.status, r.score ?? "", (r.review || []).map((i) => i.name).join(";"), ...cols.map((c) => (r.responses || {})[c] ?? "")]
        .map(csvCell)
        .join(",")
    );
  }
  download("scan_results.csv", lines.join("\n") + "\n");
}
