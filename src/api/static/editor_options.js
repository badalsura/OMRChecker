// Template editor: GUI controls for options that used to be JSON-only
// (OCR layout, barcode fallback and decoders, review thresholds
// with the fill histogram, alignment method, learned models, PDF rendering,
// output column order). Expert tuning stays in the config.json box.
import { add, api, el, state, toast } from "./api.js";
import { normalizeDropdown } from "./editor_checks.js";
import { help } from "./editor_help.js";

// Tesseract page segmentation modes offered by name (item 1)
export const PSM_CHOICES = [
  [7, "One line (default)", "A single printed field, e.g. Sr No"],
  [8, "One word", "A short code with no spaces"],
  [6, "Block of lines", "A multi-line address or name block"],
  [11, "Find text anywhere", "Text whose position varies, e.g. digits under a pasted barcode sticker"],
  [13, "Raw line, no layout checks", "A line Tesseract keeps splitting wrongly"],
];


const REVIEW_DEFAULTS = { min_confidence: 0.35, min_marked_fill_ratio: 0.25, max_unmarked_fill_ratio: 0.6, confidence_margin: 20, min_marked_bubbles: 0 };
const REVIEW_FLAGS = [
  ["multi_marked", "two or more bubbles marked"],
  ["empty", "nothing marked"],
  ["ambiguous_threshold", "marked/unmarked split unclear"],
  ["low_confidence", "low confidence"],
  ["weak_mark", "weak mark"],
  ["possible_missed_mark", "possible missed mark"],
  ["model_disagrees", "bubble model disagrees"],
  ["rectify_failed", "printed border not found"],
];
const DEFAULT_REVIEW_FLAGS = ["multi_marked", "ambiguous_threshold", "low_confidence", "weak_mark", "possible_missed_mark", "model_disagrees"];
const BARCODE_ENGINES = ["zxing", "builtin", "opencv", "pyzbar"];

const labeled = (label, control, ctx) => help(el("label", { class: "field" }, label, control), label, ctx);
const ticked = (label, checked, onchange, ctx) => {
  const cb = el("input", { type: "checkbox", checked: !!checked });
  cb.addEventListener("change", () => onchange(cb.checked));
  return help(el("label", { class: "field inline-check" }, cb, ` ${label}`), label, ctx);
};
const select = (options, value, onchange) => {
  const sel = el("select", {}, options.map(([v, t]) => el("option", { value: String(v), selected: String(v) === String(value ?? "") }, t)));
  sel.addEventListener("change", () => onchange(sel.value));
  return sel;
};

function section(ed, key, title, ...children) {
  const details = el("details", { class: "ed-section", open: ed.openSections.has(key) || undefined });
  details.addEventListener("toggle", () => (details.open ? ed.openSections.add(key) : ed.openSections.delete(key)));
  add(details, el("summary", {}, title), ...children);
  return details;
}

// ------------------------------------------------------------------ OCR zone
export function psmControl(value, onchange) {
  const known = PSM_CHOICES.some(([v]) => v === value);
  const options = [["", "One line (default)"], ...PSM_CHOICES.filter(([v]) => v !== 7).map(([v, t]) => [v, t]), ...(value !== undefined && value !== null && !known ? [[value, `mode ${value} (set in JSON)`]] : [])];
  const sel = select(options, value === 7 ? "" : value, (v) => onchange(v === "" ? null : Number(v)));
  const hint = el("div", { class: "muted small" }, (PSM_CHOICES.find(([v]) => v === (value ?? 7)) || [])[2] || "");
  sel.addEventListener("change", () => (hint.textContent = (PSM_CHOICES.find(([v]) => String(v) === (sel.value || "7")) || [])[2] || ""));
  return el("div", { class: "stack" }, sel, hint);
}

// ------------------------------------------------------------------ barcode zone
export function renderBarcodeOptions(ed, name, opts, setOpt) {
  const zones = Object.entries(ed.doc.zones || {}).filter(([n, z]) => n !== name && (z.type === "ocr" || z.type === "icr"));
  const out = [
    labeled("If the barcode fails, read this zone", select([["", "nothing (no fallback)"], ...zones.map(([n, z]) => [n, `${n} (${z.type})`])], opts.fallbackZone || "", (v) => setOpt("fallbackZone", v || null)), "zone"),
  ];
  if (opts.fallbackZone) {
    out.push(
      labeled("Clean-up before comparing", normalizeDropdown(opts.fallbackNormalize ?? "strip", (v) => setOpt("fallbackNormalize", v === "strip" ? null : v ?? "none")), "zone"),
      ticked("Send to review when the fallback is used", opts.reviewOnFallback !== false, (v) => setOpt("reviewOnFallback", v ? null : false), "zone")
    );
  }
  const engines = opts.engines || [];
  const engineRows = el("div", { class: "formats one" });
  const order = engines.length ? [...engines, ...BARCODE_ENGINES.filter((e) => !engines.includes(e))] : BARCODE_ENGINES;
  order.forEach((engine, i) => {
    const on = !engines.length || engines.includes(engine);
    const cb = el("input", { type: "checkbox", checked: on });
    const apply = (list) => setOpt("engines", list.length && list.join() !== BARCODE_ENGINES.join() ? list : null);
    cb.addEventListener("change", () => {
      const current = (engines.length ? engines : BARCODE_ENGINES).slice();
      apply(cb.checked ? order.filter((e) => current.includes(e) || e === engine) : current.filter((e) => e !== engine));
    });
    const up = el("button", { class: "small ghost", title: "Try earlier", disabled: i === 0 || undefined, onclick: () => {
      const list = order.slice();
      [list[i - 1], list[i]] = [list[i], list[i - 1]];
      apply(list.filter((e) => !engines.length || engines.includes(e)));
    } }, "↑");
    engineRows.append(el("label", { class: "row gap" }, cb, ` ${engine}`, el("span", { class: "spacer" }), up));
  });
  out.push(
    section(
      ed,
      `barcode-adv:${name}`,
      "Barcode advanced",
      labeled("Decoders, in order", engineRows, "zone"),
      ticked("Use pyzbar (ZBar) when installed", opts.pyzbar, (v) => setOpt("pyzbar", v || null), "zone"),
      ticked("Code 39 check digit", opts.code39Checksum, (v) => setOpt("code39Checksum", v || null), "zone"),
      labeled("Code 39 full ASCII", select([["auto", "auto"], ["true", "on"], ["false", "off"]], opts.code39Extended === undefined ? "auto" : String(opts.code39Extended), (v) => setOpt("code39Extended", v === "auto" ? null : v === "true")), "zone"),
      ticked("ITF check digit", opts.itfChecksum, (v) => setOpt("itfChecksum", v || null), "zone"),
      labeled("ITF minimum length", numberInput(opts.itfMinLength, (v) => setOpt("itfMinLength", v === null ? null : Math.max(2, Math.round(v))), "default"), "zone")
    )
  );
  return out;
}

// Lazy reading for OCR/ICR zones that serve as a fallback
export function renderLazyOption(ed, name, opts, setOpt) {
  const usedAsFallback = Object.values(ed.doc.zones || {}).some((z) => z.options?.fallbackZone === name);
  const usedByCheck = (ed.doc.checks || []).some((c) => (c.sources || []).includes(name));
  if (!usedAsFallback && !usedByCheck) return null;
  return ticked("Read only when needed", usedAsFallback ? opts.lazy !== false : !!opts.lazy, (v) => setOpt("lazy", usedAsFallback ? (v ? null : false) : v || null), "zone");
}

function numberInput(value, onchange, placeholder = "", step = "any") {
  const input = el("input", { type: "number", value: value ?? "", placeholder, step });
  input.addEventListener("change", () => onchange(input.value === "" ? null : Number(input.value)));
  return input;
}

// ------------------------------------------------------------------ review thresholds
function slider(label, value, fallback, { min = 0, max = 1, step = 0.01 }, onchange) {
  const shown = value ?? fallback;
  const range = el("input", { type: "range", min, max, step, value: shown });
  const num = el("input", { type: "number", min, max, step, value: shown, class: "slider-num" });
  range.addEventListener("input", () => (num.value = range.value));
  range.addEventListener("change", () => onchange(Number(range.value)));
  num.addEventListener("change", () => onchange(num.value === "" ? null : Number(num.value)));
  return labeled(label, el("div", { class: "row gap slider" }, range, num, value === undefined ? el("span", { class: "muted small" }, "default") : null), "review");
}

export function renderReviewParams(ed) {
  const config = ed.configObject() || {};
  const r = config.review_params || {};
  const set = (key, value) => ed.setConfig("review_params", { [key]: value === REVIEW_DEFAULTS[key] ? null : value });
  const flags = r.review_flags || DEFAULT_REVIEW_FLAGS;
  const flagBox = el("div", { class: "formats one" });
  for (const [flag, text] of REVIEW_FLAGS) {
    const cb = el("input", { type: "checkbox", checked: flags.includes(flag) });
    cb.addEventListener("change", () => {
      const next = REVIEW_FLAGS.map(([f]) => f).filter((f) => (f === flag ? cb.checked : flags.includes(f)));
      ed.setConfig("review_params", { review_flags: next.join() === DEFAULT_REVIEW_FLAGS.join() ? null : next });
    });
    flagBox.append(el("label", {}, cb, ` ${text}`, el("span", { class: "muted small" }, ` ${flag}`)));
  }
  return section(
    ed,
    "review",
    "Review thresholds",
    renderHistogram(ed, r),
    slider("Min confidence", r.min_confidence, REVIEW_DEFAULTS.min_confidence, {}, (v) => set("min_confidence", v)),
    slider("Weak mark below", r.min_marked_fill_ratio, REVIEW_DEFAULTS.min_marked_fill_ratio, {}, (v) => set("min_marked_fill_ratio", v)),
    slider("Possible missed mark above", r.max_unmarked_fill_ratio, REVIEW_DEFAULTS.max_unmarked_fill_ratio, {}, (v) => set("max_unmarked_fill_ratio", v)),
    slider("Confidence margin", r.confidence_margin, REVIEW_DEFAULTS.confidence_margin, { min: 1, max: 80, step: 1 }, (v) => set("confidence_margin", v)),
    labeled("Min marked bubbles per sheet", numberInput(r.min_marked_bubbles, (v) => set("min_marked_bubbles", v === null ? null : Math.max(0, Math.round(v))), "0 = off", "1"), "review"),
    labeled("Flags that send a field to review", flagBox, "review")
  );
}

// Fill-ratio histogram of the last test read: marked vs unmarked bubbles,
// with the weak-mark and missed-mark lines
function renderHistogram(ed, r) {
  const fields = ed.testResult?.fields;
  if (!fields) return el("p", { class: "muted small" }, "Run \"Test read…\" on a sample sheet to see its bubble fill histogram here.");
  const bins = 25;
  const marked = new Array(bins).fill(0);
  const unmarked = new Array(bins).fill(0);
  for (const f of Object.values(fields)) {
    for (const b of f.bubbles || []) {
      if (typeof b.fill_ratio !== "number") continue;
      const i = Math.min(bins - 1, Math.floor(b.fill_ratio * bins));
      (b.marked ? marked : unmarked)[i]++;
    }
  }
  const W = 300, H = 90;
  const canvas = el("canvas", { width: W * 2, height: H * 2, class: "ed-hist", title: "Bubble fill share of the last test read: green marked, grey unmarked" });
  const ctx = canvas.getContext("2d");
  ctx.scale(2, 2);
  const peak = Math.max(1, ...marked, ...unmarked);
  const scale = (n) => (n ? Math.max(2, (Math.log(1 + n) / Math.log(1 + peak)) * (H - 16)) : 0);
  const bw = W / bins;
  for (let i = 0; i < bins; i++) {
    ctx.fillStyle = "rgba(130,138,155,0.75)";
    ctx.fillRect(i * bw + 1, H - 12 - scale(unmarked[i]), bw / 2 - 1, scale(unmarked[i]));
    ctx.fillStyle = "rgba(31,157,85,0.85)";
    ctx.fillRect(i * bw + bw / 2, H - 12 - scale(marked[i]), bw / 2 - 1, scale(marked[i]));
  }
  const line = (x, color, text) => {
    ctx.strokeStyle = color;
    ctx.setLineDash([3, 2]);
    ctx.beginPath();
    ctx.moveTo(x * W, 0);
    ctx.lineTo(x * W, H - 12);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = "9px system-ui, sans-serif";
    ctx.fillText(text, Math.min(W - 60, x * W + 2), 9);
  };
  line(r.min_marked_fill_ratio ?? REVIEW_DEFAULTS.min_marked_fill_ratio, "#c77800", "weak mark");
  line(r.max_unmarked_fill_ratio ?? REVIEW_DEFAULTS.max_unmarked_fill_ratio, "#d0342c", "missed mark");
  ctx.fillStyle = "#6b7385";
  ctx.font = "9px system-ui, sans-serif";
  ctx.fillText("0", 0, H - 2);
  ctx.fillText("fill share", W / 2 - 18, H - 2);
  ctx.fillText("1", W - 6, H - 2);
  return el("div", {}, canvas, el("div", { class: "muted small" }, `${marked.reduce((a, b) => a + b, 0)} marked (green), ${unmarked.reduce((a, b) => a + b, 0)} unmarked (grey) bubbles in ${ed.testResult.file_id || "the test sheet"}`));
}

// ------------------------------------------------------------------ alignment
const ALIGN_METHODS = [
  ["", "None (sheets are already straight)"],
  ["TimingMarkAlignment", "Timing tracks"],
  ["CropOnMarkers", "Corner markers"],
  ["CropPage", "Page edges"],
  ["FeatureBasedAlignment", "Match a reference image (features)"],
  ["EccAlignment", "Match a reference image (ECC)"],
];
const ALIGN_NAMES = ALIGN_METHODS.map(([v]) => v).filter(Boolean);

// Other workstreams add index-point / track editors here: fn(editor, processor) -> Node|null
export const alignmentExtensions = [];
export function registerAlignmentExtension(fn) {
  alignmentExtensions.push(fn);
}

function defaultOptions(name, ed) {
  const ref = ed.detail?.reference_url ? (ed.doc.preProcessors || []).map((p) => p.options?.reference || p.options?.relativePath).find(Boolean) || "reference.png" : "reference.png";
  switch (name) {
    case "TimingMarkAlignment":
      return { tracks: {}, markDimensions: [20, 10] };
    case "CropOnMarkers":
      return { relativePath: "omr_marker.jpg", sheetToMarkerWidthRatio: 17 };
    case "CropPage":
      return { morphKernel: [10, 10] };
    case "FeatureBasedAlignment":
      return { reference: ref, maxFeatures: 1000, goodMatchPercent: 0.15 };
    case "EccAlignment":
      return { reference: ref, motion: "homography" };
    default:
      return {};
  }
}

export function renderAlignment(ed) {
  const doc = ed.doc;
  const list = doc.preProcessors || [];
  const index = list.findIndex((p) => ALIGN_NAMES.includes(p.name));
  const proc = index >= 0 ? list[index] : null;
  ed.alignStash = ed.alignStash || {};
  const setMethod = (name) =>
    ed.edit(() => {
      const next = (doc.preProcessors || []).slice();
      if (proc) ed.alignStash[proc.name] = JSON.parse(JSON.stringify(proc));
      const created = name ? ed.alignStash[name] || { name, options: defaultOptions(name, ed) } : null;
      if (index >= 0) {
        if (created) next[index] = created;
        else next.splice(index, 1);
      } else if (created) {
        // Alignment runs after clean-up filters but before anything else
        next.push(created);
      }
      doc.preProcessors = next;
      if (name === "TimingMarkAlignment" && !Object.keys(created.options.tracks || {}).length) {
        toast("Timing tracks: draw the tracks (track editor) or paste them into the preProcessors JSON before saving", "", 6000);
      }
    });
  const setOpt = (key, value) =>
    ed.edit(() => {
      const target = doc.preProcessors[index];
      target.options = { ...(target.options || {}) };
      if (value === null || value === undefined || value === "") delete target.options[key];
      else target.options[key] = value;
    });
  const o = proc?.options || {};
  const num = (label, key, placeholder, step = "any") => labeled(label, numberInput(o[key], (v) => setOpt(key, v), placeholder, step), "align");
  const tick = (label, key) => ticked(label, o[key], (v) => setOpt(key, v || null), "align");
  const text = (label, key, placeholder) => {
    const input = el("input", { value: o[key] ?? "", placeholder });
    input.addEventListener("change", () => setOpt(key, input.value.trim() || null));
    return labeled(label, input, "align");
  };
  const body = [labeled("Alignment method", select(ALIGN_METHODS, proc?.name || "", setMethod), "align")];
  const others = list.filter((p) => !ALIGN_NAMES.includes(p.name)).map((p) => p.name);
  if (proc?.name === "TimingMarkAlignment") {
    const tracks = Object.entries(o.tracks || {});
    body.push(
      el("div", { class: "muted small" }, tracks.length ? `Tracks: ${tracks.map(([n, t]) => `${n} (${(t.marks || []).length} marks)`).join(", ")} · mark ${(o.markDimensions || []).join(" × ")} px` : "No tracks defined yet."),
      num("Search radius", "searchRadius", "default"),
      num("Minimum matched marks", "minMatchedMarks", "default", "1"),
      num("Max leftover error", "maxResidual", "default"),
      // upside-down detection: the "Detect upside-down sheets" switch with the tracks below
      tick("Bend to fit the marks (nonRigid)", "nonRigid")
    );
  } else if (proc?.name === "CropOnMarkers") {
    body.push(text("Marker image", "relativePath", "omr_marker.jpg"), num("Marker width share", "sheetToMarkerWidthRatio", "17"), num("Min match score", "min_matching_threshold", "default"));
  } else if (proc?.name === "CropPage") {
    const k = o.morphKernel || [10, 10];
    body.push(labeled("Edge kernel", numberInput(k[0], (v) => setOpt("morphKernel", v === null ? null : [Math.max(1, Math.round(v)), Math.max(1, Math.round(v))]), "10", "1"), "align"));
  } else if (proc?.name === "FeatureBasedAlignment") {
    body.push(text("Reference image", "reference", "reference.png"), num("Max features", "maxFeatures", "1000", "1"), num("Good matches share", "goodMatchPercent", "0.15"), tick("Only shift and rotate (2d)", "2d"));
  } else if (proc?.name === "EccAlignment") {
    body.push(text("Reference image", "reference", "reference.png"), labeled("Motion model", select([["translation", "shift only"], ["euclidean", "shift and rotate"], ["affine", "affine"], ["homography", "full perspective"]], o.motion || "homography", (v) => setOpt("motion", v)), "align"));
  }
  for (const fn of alignmentExtensions) {
    try {
      const node = fn(ed, proc);
      if (node) body.push(node);
    } catch (e) {
      console.error(e);
    }
  }
  if (others.length) body.push(el("div", { class: "muted small" }, `Clean-up steps kept: ${others.join(", ")} (edit in the preProcessors JSON).`));
  body.push(el("div", { class: "muted small" }, "Run \"Test read…\" and pick \"Last test scan\" as background to see the aligned result under the template."));
  return section(ed, "alignment", `Alignment${proc ? ` · ${ALIGN_METHODS.find(([v]) => v === proc.name)[1]}` : ""}`, ...body);
}

// ------------------------------------------------------------------ models, PDF, barcode defaults
let modelCache = {};
export function renderModels(ed) {
  const config = ed.configObject() || {};
  const ml = config.ml_params || {};
  const box = el("div", {});
  const build = (models) => {
    box.innerHTML = "";
    const picker = (label, key, kind) => {
      const current = ml[key] || "";
      const fits = models.filter((m) => !m.kind || m.kind === kind);
      const options = [["", "none (classical reading)"], ...fits.map((m) => [m.path, `${m.name}${m.location === "server" ? " (server)" : ""}`])];
      if (current && !options.some(([v]) => v === current)) options.push([current, `${current} (not found)`]);
      return labeled(label, select(options, current, (v) => ed.setConfig("ml_params", { [key]: v || null })), "ml");
    };
    add(box, 
      picker("Bubble model", "bubble_model_path", "bubble"),
      picker("Handwriting model", "icr_model_path", "icr"),
      state.caps && state.caps.onnxruntime === false ? el("div", { class: "muted small" }, "onnxruntime is not installed on this server: models are ignored.") : null,
      models.length ? null : el("div", { class: "muted small" }, "No .onnx models found. Upload one with the template files, or put it in the server's models folder.")
    );
  };
  build(modelCache[ed.id] || []);
  if (!modelCache[ed.id]) {
    api(`/templates/${ed.id}/models`)
      .then((data) => {
        modelCache[ed.id] = data.models || [];
        build(modelCache[ed.id]);
      })
      .catch(() => build([]));
  }
  return section(ed, "models", "Learned models", box);
}

export const PDF_DPI_CHOICES = [["auto", "auto (the PDF's own resolution)"], [150, "150"], [200, "200"], [300, "300"], [400, "400"], [600, "600"]];

export function renderPdfParams(ed) {
  const config = ed.configObject() || {};
  const p = config.pdf_params || {};
  const page = p.pdf_page === undefined ? 1 : p.pdf_page;
  const pageText = page === null ? "all" : Array.isArray(page) ? page.join(",") : String(page);
  const input = el("input", { value: pageText, placeholder: "1" });
  input.addEventListener("change", () => {
    const v = input.value.replace(/\s/g, "").toLowerCase();
    if (!v || v === "1") return ed.setConfig("pdf_params", { pdf_page: null });
    if (v === "all") return ed.setConfig("pdf_params", { pdf_page: "1-" });
    if (!/^\d+(-\d*)?(,\d+(-\d*)?)*$/.test(v)) return toast("Pages look like 1, 2-4, 3- or all", "error");
    const parts = v.split(",").map((x) => (/^\d+$/.test(x) ? Number(x) : x));
    ed.setConfig("pdf_params", { pdf_page: parts.length === 1 ? parts[0] : parts });
  });
  return section(
    ed,
    "pdf",
    "PDF input",
    labeled("PDF render DPI", select(PDF_DPI_CHOICES, p.pdf_dpi ?? "auto", (v) => ed.setConfig("pdf_params", { pdf_dpi: v === "auto" ? null : Number(v) })), "pdf"),
    labeled("PDF pages", input, "pdf"),
    el("div", { class: "muted small" }, "Template default; the Scan and New Job screens can override it per batch.")
  );
}

export function renderBarcodeDefaults(ed) {
  const config = ed.configObject() || {};
  const b = config.barcode_params || {};
  const engines = b.engines || BARCODE_ENGINES;
  const box = el("div", { class: "formats one" });
  engines.concat(BARCODE_ENGINES.filter((e) => !engines.includes(e))).forEach((engine, i, order) => {
    const cb = el("input", { type: "checkbox", checked: engines.includes(engine) });
    const apply = (list) => ed.setConfig("barcode_params", { engines: list.join() === BARCODE_ENGINES.join() ? null : list });
    cb.addEventListener("change", () => {
      const next = order.filter((e) => (e === engine ? cb.checked : engines.includes(e)));
      if (!next.length) return toast("Keep at least one decoder", "error");
      apply(next);
    });
    const up = el("button", { class: "small ghost", disabled: i === 0 || undefined, onclick: () => {
      const list = order.slice();
      [list[i - 1], list[i]] = [list[i], list[i - 1]];
      apply(list.filter((e) => engines.includes(e)));
    } }, "↑");
    add(box, el("label", { class: "row gap" }, cb, ` ${engine}`, el("span", { class: "spacer" }), up));
  });
  return section(
    ed,
    "barcode-defaults",
    "Barcode advanced",
    labeled("Decoders, in order", box, "barcode"),
    ticked("Use pyzbar (ZBar) when installed", b.pyzbar, (v) => ed.setConfig("barcode_params", { pyzbar: v || null }), "barcode"),
    ticked("Review reads by fallback decoders", b.review_fallback_decodes, (v) => ed.setConfig("barcode_params", { review_fallback_decodes: v || null }), "barcode")
  );
}

// ------------------------------------------------------------------ output columns
// Every column the template exports, in the engine's default (natural) order
export function allOutputColumns(ed) {
  const doc = ed.doc;
  const grouped = new Set(Object.keys(doc.customLabels || {}).flatMap((g) => ed.groupColumns(g)));
  const names = [...Object.keys(doc.customLabels || {}), ...[...ed.allLabels()].filter((l) => !grouped.has(l)), ...Object.keys(doc.zones || {}).filter((z) => !grouped.has(z))];
  for (const c of doc.checks || []) {
    const out = c.output || c.name;
    if (out && !names.includes(out)) names.push(out);
  }
  return names.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

export function renderOutputColumns(ed) {
  const doc = ed.doc;
  const custom = Array.isArray(doc.outputColumns) && doc.outputColumns.length > 0;
  const all = allOutputColumns(ed);
  const chosen = custom ? ed.expandLabels(doc.outputColumns) : [];
  const list = el("ol", { class: "ed-drag-list" });
  const write = (next) => ed.edit(() => (next && next.length ? (doc.outputColumns = ed.compressLabels(next)) : delete doc.outputColumns));
  let dragFrom = null;
  const items = custom ? [...chosen, ...all.filter((c) => !chosen.includes(c))] : all;
  items.forEach((name, i) => {
    const on = !custom || chosen.includes(name);
    const cb = el("input", { type: "checkbox", checked: on, disabled: !custom || undefined, title: "Export this column" });
    cb.addEventListener("change", () => write(cb.checked ? [...chosen.slice(0, chosen.length), name] : chosen.filter((c) => c !== name)));
    const li = el("li", { draggable: custom ? "true" : undefined, class: on ? "" : "off" }, custom ? el("span", { class: "grip" }, "⋮⋮") : null, cb, el("span", { class: "mono" }, name));
    if (custom && on) {
      const pos = chosen.indexOf(name);
      li.addEventListener("dragstart", (e) => { dragFrom = pos; try { e.dataTransfer.setData("text/plain", name); } catch (err) { /* ignore */ } });
      li.addEventListener("dragover", (e) => e.preventDefault());
      li.addEventListener("drop", (e) => {
        e.preventDefault();
        if (dragFrom === null || pos < 0) return;
        const next = chosen.slice();
        const [x] = next.splice(dragFrom, 1);
        next.splice(pos, 0, x);
        dragFrom = null;
        write(next);
      });
    }
    list.append(li);
  });
  const missing = custom ? all.filter((c) => !chosen.includes(c)).length : 0;
  return section(
    ed,
    "output",
    `Output columns${custom ? " · custom order" : ""}`,
    ticked("Custom column order", custom, (v) => write(v ? all : null), "output"),
    custom ? el("div", { class: "muted small" }, `Drag to reorder; untick to leave a column out${missing ? ` (${missing} left out)` : ""}.`) : el("div", { class: "muted small" }, "All columns, sorted by name."),
    list
  );
}
