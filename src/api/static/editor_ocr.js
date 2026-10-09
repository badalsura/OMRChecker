// Zone editor additions for OCR / handwriting / image zones (src/readers):
// text direction with a preview of the crop as the engine sees it, OCR engine
// and fallback, language dropdown, layout-mode retry, user patterns, per-
// character confidence, and the image zone's file name / base64 switch.
// Used by editor.js: renderZone() appends ocrZoneControls(...).
import { api, el } from "./api.js";

const DIRECTIONS = [
  ["horizontal", "Horizontal (normal)"],
  ["rot90cw", "Vertical, reading bottom to top"],
  ["rot90ccw", "Vertical, reading top to bottom"],
  ["rot180", "Upside down"],
  ["auto", "Auto: try all four (slower)"],
];

let capsPromise = null;
// Engines and installed languages on this server (GET /ocr/capabilities), cached
export function ocrCapabilities() {
  if (!capsPromise) capsPromise = api("/ocr/capabilities").catch(() => null);
  return capsPromise;
}

// Label + control + an info icon; the help text shows while the control has focus
function withHelp(label, control, help) {
  const text = el("div", { class: "muted small", style: { display: "none", marginTop: "2px" } }, help);
  const icon = el("span", { title: help, style: { cursor: "help", marginLeft: "4px", opacity: "0.7" }, tabindex: "0" }, "ⓘ");
  const show = (on) => (text.style.display = on ? "" : "none");
  const wrap = el("label", { class: "field" }, el("span", {}, label, icon), control, text);
  wrap.addEventListener("focusin", () => show(true));
  wrap.addEventListener("focusout", () => show(false));
  icon.addEventListener("click", (e) => {
    e.preventDefault();
    show(text.style.display === "none");
  });
  return wrap;
}

function select(value, options, onchange) {
  const sel = el("select", {}, options.map(([v, t]) => el("option", { value: v, selected: v === value }, t)));
  sel.addEventListener("change", () => onchange(sel.value));
  return sel;
}

function checkbox(checked, onchange) {
  const cb = el("input", { type: "checkbox", checked: !!checked });
  cb.addEventListener("change", () => onchange(cb.checked));
  return cb;
}

function numberBox(value, placeholder, step, onchange, max) {
  const input = el("input", { type: "number", min: "0", step: step || "1", value: value ?? "", placeholder: placeholder || "" });
  input.addEventListener("change", () => {
    if (input.value === "") return onchange(null);
    let n = Number(input.value);
    if (!Number.isFinite(n) || n < 0) return;
    if (max !== undefined) n = Math.min(max, n);
    onchange(n);
  });
  return input;
}

function textBox(value, placeholder, onchange) {
  const input = el("input", { value: value ?? "", placeholder: placeholder || "" });
  input.addEventListener("change", () => onchange(input.value.trim()));
  return input;
}

// Draw the zone's crop of the editor background turned the way the reader turns
// it (src/readers/text_reader.py rotate_crop). With a test scan as background
// this is the aligned image the engine read, so the preview shows what was read.
export function drawDirectionPreview(canvas, bg, page, zone, direction) {
  const ctx = canvas.getContext("2d");
  const [pw, ph] = page;
  const [x, y] = zone.origin;
  const [w, h] = zone.dimensions;
  const turned = direction === "rot90cw" || direction === "rot90ccw";
  const outW = turned ? h : w;
  const outH = turned ? w : h;
  const scale = Math.min(1, 240 / Math.max(outW, 1), 120 / Math.max(outH, 1));
  canvas.width = Math.max(1, Math.round(outW * scale));
  canvas.height = Math.max(1, Math.round(outH * scale));
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (!bg) return false;
  const kx = (bg.naturalWidth || bg.width) / pw;
  const ky = (bg.naturalHeight || bg.height) / ph;
  ctx.save();
  ctx.translate(canvas.width / 2, canvas.height / 2);
  const angle = { rot90cw: 90, rot90ccw: -90, rot180: 180 }[direction] || 0;
  ctx.rotate((angle * Math.PI) / 180);
  const dw = w * scale;
  const dh = h * scale;
  ctx.drawImage(bg, x * kx, y * ky, w * kx, h * ky, -dw / 2, -dh / 2, dw, dh);
  ctx.restore();
  return true;
}

function directionPreview(editor, raw, opts, zoneName) {
  // "auto": show the direction the last test read chose, else horizontal
  const tested = editor.testResult?.zones?.[zoneName]?.details?.direction;
  const direction = opts.direction === "auto" ? tested || "horizontal" : opts.direction || "horizontal";
  const canvas = el("canvas", { style: { border: "1px solid var(--border, #ccc)", maxWidth: "100%", display: "block" } });
  const ok = drawDirectionPreview(canvas, editor.bg, editor.page(), raw, direction);
  const note = !ok
    ? "No background image: load a reference or run a test read to see the crop."
    : opts.direction === "auto"
      ? tested
        ? `Auto chose: ${DIRECTIONS.find((d) => d[0] === tested)?.[1] || tested} (last test read)`
        : "Auto: shown horizontal; run a test read to see the direction chosen."
      : "The crop as the OCR engine sees it.";
  return el("div", { class: "field" }, el("span", {}, "Crop as read"), canvas, el("div", { class: "muted small" }, note));
}

function languageOptions(caps, current) {
  const langs = (caps && caps.tesseract_languages) || [];
  const options = [["", `config default (${((caps && caps.defaults && caps.defaults.langs) || ["eng"]).join("+")})`]];
  for (const l of langs) options.push([l, l === "hin" ? "hin (Hindi)" : l === "eng" ? "eng (English)" : l]);
  if (langs.includes("eng") && langs.includes("hin")) options.push(["eng+hin", "eng+hin (English and Hindi)"]);
  if (current && !options.some(([v]) => v === current)) options.push([current, `${current} (not installed here)`]);
  return options;
}

// PaddleOCR model size and recogniser for an OCR / ICR zone; options the
// server has no model for are marked "not installed".
const PADDLE_LANGS = [
  ["en", "English"],
  ["ch", "Main (multilingual, handwriting)"],
  ["devanagari", "Devanagari (Hindi)"],
];
function paddleControls(raw, opts, setOpt) {
  const icr = raw.type === "icr";
  const sizeSel = el("span", {});
  const langSel = el("span", {});
  const render = (installed) => {
    const has = (size, lang) => !installed || installed.some((m) => (!size || m.size === size) && (!lang || m.lang === lang));
    const mark = (label, ok) => (ok ? label : `${label} (not installed)`);
    sizeSel.replaceChildren(
      select(
        opts.paddleModel || "",
        [["", "config default"], ["mobile", mark("mobile (fast)", has("mobile"))], ["server", mark("server (most accurate, slower)", has("server"))]],
        (v) => setOpt("paddleModel", v || null)
      )
    );
    langSel.replaceChildren(
      select(
        opts.paddleLang || "",
        [["", icr ? "default (main, else English)" : "default (from the language)"], ...PADDLE_LANGS.map(([v, t]) => [v, mark(t, has(null, v))])],
        (v) => setOpt("paddleLang", v || null)
      )
    );
  };
  render(null);
  ocrCapabilities().then((caps) => caps && render(caps.paddle_models || []));
  return el(
    "div",
    { class: "two" },
    withHelp("PaddleOCR model", sizeSel, "Model size PaddleOCR uses for this zone. Server models read better (handwriting especially) but are slower."),
    withHelp(
      "PaddleOCR recogniser",
      langSel,
      icr
        ? "Recogniser for this handwriting zone. The main multilingual one is trained on handwriting too; English and Devanagari are smaller."
        : "Recogniser for this zone when PaddleOCR reads it. 'Main' reads English and is trained on handwriting too."
    )
  );
}

// Controls for one zone; returns DOM nodes to append to the zone panel.
export function ocrZoneControls(editor, raw, opts, setOpt, zoneName) {
  const nodes = [];
  if (raw.type === "ocr" || raw.type === "icr") {
    nodes.push(
      withHelp(
        "Text direction",
        select(opts.direction || "horizontal", DIRECTIONS, (v) => setOpt("direction", v === "horizontal" ? null : v)),
        "Turn the crop before reading. Use a vertical setting for text printed sideways (e.g. a scanner-imprinted number). Auto reads all four ways and keeps the valid read with the highest confidence; it is slower."
      ),
      directionPreview(editor, raw, opts, zoneName)
    );
  }
  if (raw.type === "ocr" || raw.type === "icr") nodes.push(paddleControls(raw, opts, setOpt));
  if (raw.type === "ocr") {
    const engines = [["default", "config default"], ["tesseract", "Tesseract"], ["paddle", "PaddleOCR"]];
    const fallbacks = [["default", "config default"], ["none", "none"], ["tesseract", "Tesseract"], ["paddle", "PaddleOCR"]];
    const langHolder = el("span", {}, textBox(opts.lang, "eng", (v) => setOpt("lang", v || null)));
    const status = el("div", { class: "muted small" });
    ocrCapabilities().then((caps) => {
      if (!caps) return;
      langHolder.replaceChildren(select(opts.lang || "", languageOptions(caps, opts.lang), (v) => setOpt("lang", v || null)));
      if (!caps.engines?.paddle) status.textContent = `PaddleOCR not available on this server${caps.paddle_reason ? `: ${caps.paddle_reason}` : ""}; zones set to it use Tesseract.`;
    });
    nodes.push(
      withHelp("Language", langHolder, "Tesseract language model for this zone. Only languages installed on the server are listed; 'eng+hin' reads both."),
      el(
        "div",
        { class: "two" },
        withHelp("OCR engine", select(opts.engine || "default", engines, (v) => setOpt("engine", v === "default" ? null : v)), "Engine that reads this zone. 'config default' uses ocr_params.default_engine."),
        withHelp(
          "Fallback engine",
          select(opts.fallbackEngine || "default", fallbacks, (v) => setOpt("fallbackEngine", v === "default" ? null : v)),
          "Second engine, tried when the first read is empty, below Min confidence or fails the pattern. The better valid read wins; if the two engines read different text the zone goes to review."
        )
      ),
      status,
      withHelp(
        "Retry other layout modes",
        select(opts.psmRetry === undefined ? "" : String(opts.psmRetry), [["", "config default (on)"], ["true", "on"], ["false", "off"]], (v) => setOpt("psmRetry", v === "" ? null : v === "true")),
        "If the read is empty or fails the pattern, try other Tesseract page segmentation modes and keep the best valid result."
      ),
      withHelp(
        "Expected format for Tesseract (user patterns)",
        textBox((opts.userPatterns || []).join(", "), "from the pattern, e.g. \\d\\d\\d\\d\\d\\d\\d", (v) => setOpt("userPatterns", v ? v.split(",").map((s) => s.trim()).filter(Boolean) : null)),
        "Tesseract user patterns, comma separated: \\d digit, \\A capital, \\a lower-case letter, \\* repeat. Left empty, simple patterns such as [0-9]{7} are converted automatically."
      ),
      withHelp(
        "Min character confidence",
        numberBox(opts.minCharConfidence, "off", "0.05", (v) => setOpt("minCharConfidence", v || null), 1),
        "Send the zone to review when any single character is below this confidence (0 to 1), so one doubtful digit is checked by hand. Empty = off."
      )
    );
  }
  if (raw.type === "image") {
    nodes.push(
      withHelp(
        "File name",
        textBox(opts.saveFilename, "{file}_{zone}.png", (v) => setOpt("saveFilename", v || null)),
        "Name of the saved crop. {file} is the sheet's file name, {zone} this zone's name, {page} the PDF page. The result keeps this file name."
      ),
      withHelp("Longest side (px)", numberBox(opts.maxSide, "full size", "1", (v) => setOpt("maxSide", v ? Math.max(16, Math.round(v)) : null)), "Shrink the saved crop so its longer side is at most this many pixels."),
      withHelp(
        "Embed image in results (base64)",
        el("span", {}, checkbox(opts.embedBase64, (v) => setOpt("embedBase64", v || null)), " store the crop in the database record and API responses"),
        "Off: the crop is saved as a file and the result holds its name. On: a small copy is also embedded as base64, so the database and API carry the image itself. Only small crops are embedded."
      )
    );
  }
  return nodes;
}
