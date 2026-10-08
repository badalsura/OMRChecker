// Template editor: page alignment options and the block border preview
// (plan items 13, 14, 36, 40). Plain ES module, no build step, works offline.
//
//   alignmentSection(editor)            Page panel: switches stored under the
//                                       template's "alignment" key
//   blockAlignmentFields(editor, raw)   Block panel: two-level search gap and
//                                       bubble-outline fit for one block
//   AlignPanel                          Preview of detected block borders on the
//                                       reference / sample sheets + "Test on samples"
//
// Everything drawn comes from the read's stored geometry, like the Results screen.
import { api, el, toast } from "./api.js";

const OK_COLOR = "#1e9e3a";
const FAIL_COLOR = "#d62828";
const BUBBLE_COLOR = "rgba(47, 111, 223, 0.8)";
const MAX_CANVAS_WIDTH = 900;

const HELP = {
  rectify_on_border:
    "After page alignment, find the rectangle printed around each block and map its bubbles onto it. Corrects a block that sits slightly off, rotated or skewed. A block's own setting overrides this. If the border is not found, the page alignment is kept and the block is flagged rectify_failed.",
  block_perspective:
    "For blocks without a printed box: fit the block's perspective to its printed bubble outlines. Uses the same safety limits as border fitting; a fit that moves too far or makes the bubbles fit worse is rejected.",
  rectify_print_image:
    "Which image borders are searched on when colour removal drops the printed form (pink / red forms): the darkest channel keeps coloured print dark, plain grey is the ordinary grey copy. Bubbles are still read on the colour-removed image; both share one alignment.",
  rectify_search_px: "How far (pixels) a block border may sit from where the template expects it.",
  verify_bubble_fit:
    "Reject a block correction when the printed bubbles fit worse after it than before. Keep this on: turning it off can hide misaligned blocks.",
  page_outline:
    "For phone photos: find the page's four corners and flatten the photo before alignment. Flatbed scans without a visible page edge are left unchanged.",
  outerBorderPadding:
    "Two-level search: gap (px) from this block's bubbles to a large outer frame printed around it. The outer frame is found first, then the block's own border is searched relative to it. Leave empty for a single-level search.",
  blockPerspective:
    "Fit this block to its printed bubble outlines when it has no printed box (or the box is not found).",
  showOutlines: "Draw each block's detected border: green when found and used, red when it failed.",
};

function ensureStyles() {
  if (document.getElementById("align-css")) return;
  const style = document.createElement("style");
  style.id = "align-css";
  style.textContent = `
.al-field { position: relative; }
.al-info { display: inline-block; margin-left: 4px; color: var(--muted, #777); cursor: help; font-style: normal; }
.al-help { display: none; font-size: 12px; color: var(--muted, #666); margin: 2px 0 6px; line-height: 1.4; }
.al-field:focus-within .al-help, .al-field:hover .al-help { display: block; }
.al-check { display: flex; align-items: center; gap: 6px; }
.al-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 50; display: flex; align-items: center; justify-content: center; }
.al-dialog { background: var(--bg, #fff); color: inherit; width: min(1250px, 96vw); height: min(900px, 94vh); border-radius: 8px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 8px 30px rgba(0,0,0,.3); }
.al-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 8px 12px; border-bottom: 1px solid var(--line, #ddd); }
.al-head h3 { margin: 0 8px 0 0; }
.al-grow { flex: 1; }
.al-body { flex: 1; display: flex; min-height: 0; }
.al-view { flex: 1; overflow: auto; padding: 8px; background: #f3f3f3; }
.al-side { width: 320px; overflow: auto; padding: 8px 12px; border-left: 1px solid var(--line, #ddd); font-size: 13px; }
.al-side table { width: 100%; border-collapse: collapse; }
.al-side td, .al-side th { border-bottom: 1px solid var(--line, #eee); padding: 3px 4px; text-align: left; vertical-align: top; }
.al-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
`;
  document.head.append(style);
}

// A labelled control with an info icon; the help text shows when focused or hovered
function helpField(label, control, help, checkbox = false) {
  const info = el("span", { class: "al-info", title: help, tabindex: "0", "aria-label": help }, "ⓘ");
  if (checkbox) {
    return el("div", { class: "field al-field" }, el("label", { class: "al-check" }, control, label, info), el("div", { class: "al-help" }, help));
  }
  return el("label", { class: "field al-field" }, el("span", {}, label, info), control, el("div", { class: "al-help" }, help));
}

// ---------------------------------------------------------------- page panel
export function alignmentSection(editor) {
  ensureStyles();
  const doc = editor.doc;
  const current = () => doc.alignment || {};
  const setKey = (key, value, fallback) =>
    editor.edit(() => {
      const next = { ...(doc.alignment || {}) };
      if (value === undefined || value === null || value === fallback) delete next[key];
      else next[key] = value;
      if (Object.keys(next).length) doc.alignment = next;
      else delete doc.alignment;
    });
  const check = (label, key, fallback) => {
    const value = current()[key];
    const box = el("input", { type: "checkbox", checked: value === undefined ? fallback : !!value });
    box.addEventListener("change", () => setKey(key, box.checked, fallback));
    return helpField(label, box, HELP[key], true);
  };
  const printSel = el(
    "select",
    {},
    [
      ["auto", "Automatic (darkest channel)"],
      ["darkest", "Darkest channel"],
      ["grey", "Plain grey"],
    ].map(([v, t]) => el("option", { value: v, selected: (current().rectify_print_image || "auto") === v }, t))
  );
  printSel.addEventListener("change", () => setKey("rectify_print_image", printSel.value, "auto"));
  const search = el("input", { type: "number", min: "2", max: "100", step: "1", value: current().rectify_search_px ?? "", placeholder: "20" });
  search.addEventListener("change", () => {
    if (search.value === "") return setKey("rectify_search_px", undefined);
    const n = Math.round(Number(search.value));
    if (!Number.isFinite(n) || n < 2 || n > 100) return toast("Border search distance must be 2 to 100 px", "error");
    setKey("rectify_search_px", n);
  });
  return el(
    "div",
    { class: "al-section" },
    el("h3", {}, "Alignment"),
    check("Use printed block borders", "rectify_on_border", false),
    check("Fit blocks without a box to their bubbles", "block_perspective", false),
    helpField("Border search image", printSel, HELP.rectify_print_image),
    helpField("Border search distance (px)", search, HELP.rectify_search_px),
    check("Check bubbles fit after a block correction", "verify_bubble_fit", true),
    check("Find the page outline (phone photos)", "page_outline", false),
    el(
      "div",
      { class: "row gap" },
      el("button", { class: "small", title: "Show detected block borders on the reference or a sample sheet", onclick: () => new AlignPanel(editor).open() }, "Preview block borders…")
    )
  );
}

// ---------------------------------------------------------------- block panel
export function blockAlignmentFields(editor, raw) {
  ensureStyles();
  const set = (fn) => editor.edit(fn);
  const outer = el("input", {
    type: "number",
    min: "0",
    step: "1",
    value: typeof raw.outerBorderPadding === "number" ? raw.outerBorderPadding : raw.outerBorderPadding?.[0] ?? "",
    placeholder: "none (single level)",
  });
  outer.addEventListener("change", () => {
    if (outer.value === "") return set(() => delete raw.outerBorderPadding);
    const n = Number(outer.value);
    if (!Number.isFinite(n) || n < 0) return toast("Outer frame gap must be a positive number", "error");
    set(() => (raw.outerBorderPadding = n));
  });
  const fit = el(
    "select",
    {},
    [
      ["", "page default"],
      ["true", "on"],
      ["false", "off"],
    ].map(([v, t]) => el("option", { value: v, selected: (raw.blockPerspective === undefined ? "" : String(raw.blockPerspective)) === v }, t))
  );
  fit.addEventListener("change", () => set(() => (fit.value === "" ? delete raw.blockPerspective : (raw.blockPerspective = fit.value === "true"))));
  return el(
    "div",
    {},
    raw.rectifyOnBorder ? helpField("Outer frame gap (px, two-level search)", outer, HELP.outerBorderPadding) : null,
    helpField("Fit to bubble outlines (blockPerspective)", fit, HELP.blockPerspective)
  );
}

// ---------------------------------------------------------------- preview panel
export class AlignPanel {
  constructor(editor) {
    this.editor = editor;
    this.data = null;
    this.image = null;
    this.seq = 0;
    ensureStyles();
  }

  async open() {
    this.sampleSel = el("select", { class: "small", onchange: () => this.preview() });
    this.viewSel = el(
      "select",
      { class: "small", title: "Print kept: where borders are searched. Colour removed: what the bubbles are read on.", onchange: () => this.preview() },
      el("option", { value: "print" }, "Print kept (borders)"),
      el("option", { value: "dropout" }, "Colour removed (bubbles)")
    );
    this.outlines = el("input", { type: "checkbox", checked: true, onchange: () => this.draw() });
    this.bubbles = el("input", { type: "checkbox", checked: true, onchange: () => this.draw() });
    const upload = el("input", { type: "file", accept: "image/*,.pdf", multiple: true, hidden: true, onchange: (e) => this.upload(e.target.files) });
    this.canvas = el("canvas", {});
    this.side = el("div", { class: "al-side" }, el("div", { class: "muted small" }, "Loading…"));
    this.root = el(
      "div",
      { class: "al-overlay", onclick: (e) => e.target === this.root && this.close() },
      el(
        "div",
        { class: "al-dialog" },
        el(
          "div",
          { class: "al-head" },
          el("h3", {}, "Block borders"),
          el("label", { class: "small" }, "Sheet ", this.sampleSel),
          el("button", { class: "small", title: "Upload sample sheets kept with this template for previews and tests", onclick: () => upload.click() }, "Add samples…"),
          upload,
          el("label", { class: "small" }, "View ", this.viewSel),
          el("label", { class: "small al-check", title: HELP.showOutlines }, this.outlines, "Border outlines"),
          el("label", { class: "small al-check", title: "Draw the bubble positions that were read" }, this.bubbles, "Bubbles"),
          el("button", { class: "small", title: "Read the sheet again with the current (unsaved) template", onclick: () => this.preview() }, "Refresh"),
          el("button", { class: "small primary", title: "Run on every uploaded sample and list blocks whose border was not found", onclick: () => this.test() }, "Test on samples"),
          el("span", { class: "al-grow" }),
          el("button", { class: "small ghost", onclick: () => this.close() }, "Close")
        ),
        el("div", { class: "al-body" }, el("div", { class: "al-view" }, this.canvas), this.side)
      )
    );
    this.keyHandler = (e) => e.key === "Escape" && this.close();
    document.addEventListener("keydown", this.keyHandler);
    document.body.append(this.root);
    await this.loadSamples();
    this.preview();
  }

  close() {
    document.removeEventListener("keydown", this.keyHandler);
    this.root.remove();
  }

  body(extra = {}) {
    return { template: this.editor.doc, ...extra };
  }

  async loadSamples(select) {
    try {
      const data = await api(`/templates/${this.editor.id}/align/samples`);
      const options = [];
      if (data.has_reference) options.push(["reference", "Reference image"]);
      for (const name of data.samples) options.push([name, name]);
      this.sampleSel.innerHTML = "";
      for (const [v, t] of options) this.sampleSel.append(el("option", { value: v, selected: v === select }, t));
      if (!options.length) this.side.replaceChildren(el("div", { class: "muted small" }, "No reference image: add sample sheets to preview."));
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async upload(files) {
    if (!files || !files.length) return;
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    try {
      const data = await api(`/templates/${this.editor.id}/align/samples`, { method: "POST", form });
      await this.loadSamples(data.added[0]);
      this.preview();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async preview() {
    const sample = this.sampleSel.value;
    if (!sample) return;
    const seq = ++this.seq;
    this.side.replaceChildren(el("div", { class: "muted small" }, "Reading…"));
    try {
      const data = await api(`/templates/${this.editor.id}/align/preview`, { method: "POST", json: this.body({ sample, view: this.viewSel.value }) });
      if (seq !== this.seq) return;
      this.data = data;
      this.image = null;
      if (data.image_data) {
        const image = new Image();
        image.onload = () => {
          if (seq !== this.seq) return;
          this.image = image;
          this.draw();
        };
        image.src = data.image_data;
      }
      this.renderSide();
      this.draw();
    } catch (error) {
      if (seq === this.seq) this.side.replaceChildren(el("div", { class: "error" }, error.message));
    }
  }

  draw() {
    const data = this.data;
    if (!data || !data.width) return;
    const scale = Math.min(1, MAX_CANVAS_WIDTH / data.width);
    const canvas = this.canvas;
    canvas.width = Math.round(data.width * scale);
    canvas.height = Math.round(data.height * scale);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (this.image) ctx.drawImage(this.image, 0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.scale(scale, scale);
    if (this.bubbles.checked) {
      ctx.strokeStyle = BUBBLE_COLOR;
      ctx.lineWidth = 1 / scale;
      for (const bubbles of Object.values(data.fields || {})) {
        for (const b of bubbles) ctx.strokeRect(b.x, b.y, b.w, b.h);
      }
    }
    if (this.outlines.checked) {
      for (const block of data.blocks || []) {
        if (!block.corners) continue;
        ctx.strokeStyle = block.used ? OK_COLOR : FAIL_COLOR;
        ctx.lineWidth = 2.5 / scale;
        ctx.beginPath();
        block.corners.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.closePath();
        ctx.stroke();
      }
    }
    ctx.restore();
  }

  renderSide() {
    const data = this.data;
    const rows = (data.blocks || []).map((b) =>
      el(
        "tr",
        {},
        el("td", {}, el("span", { class: "al-dot", style: { background: b.used ? OK_COLOR : FAIL_COLOR } }), b.name),
        el("td", {}, `${b.status}${b.method === "bubbles" ? " (bubbles)" : ""}${b.level ? ` · ${b.level}` : ""}`),
        el("td", { class: "muted" }, b.reason || "")
      )
    );
    const residual = data.residual;
    const trim = data.margin_trim;
    this.side.replaceChildren(
      ...[
      el("h4", {}, `${data.sample}: ${data.status}`),
      data.error ? el("div", { class: "error" }, data.error) : null,
      rows.length
        ? el("table", {}, el("tr", {}, el("th", {}, "Block"), el("th", {}, "Border"), el("th", {}, "Reason")), rows)
        : el("div", { class: "muted small" }, "No block uses border or bubble fitting. Turn on \"Use printed block borders\" on the Page panel or on a block."),
      residual ? el("p", { class: "muted small" }, `Page alignment residual: ${residual.page} px`) : null,
      trim ? el("p", { class: "muted small" }, `Trimmed margin (template px): top ${trim.top}, bottom ${trim.bottom}, left ${trim.left}, right ${trim.right}`) : null,
      ...(data.sheet_review || []).map((item) => el("div", { class: "error small" }, `${item.name}: ${(item.flags || []).join(", ")}${item.missing ? ` (${item.missing.join(", ")})` : ""}`)),
      (this.testBox = this.testBox || el("div", {})),
      ].filter(Boolean)
    );
  }

  async test() {
    const box = (this.testBox = this.testBox || el("div", {}));
    if (!box.isConnected) this.side.append(box);
    box.replaceChildren(el("div", { class: "muted small" }, "Testing every sample…"));
    try {
      const data = await api(`/templates/${this.editor.id}/align/test`, { method: "POST", json: this.body() });
      const rows = data.samples.map((row) =>
        el(
          "tr",
          {},
          el("td", {}, row.sample),
          el(
            "td",
            {},
            row.error
              ? el("span", { class: "error" }, row.error)
              : row.failed_blocks.length
              ? row.failed_blocks.map((b) => el("div", {}, el("span", { class: "al-dot", style: { background: FAIL_COLOR } }), `${b.name}: ${b.reason}`))
              : el("span", {}, el("span", { class: "al-dot", style: { background: OK_COLOR } }), "all found")
          )
        )
      );
      box.replaceChildren(
        el("h4", {}, `Test on samples: ${data.total - data.failed}/${data.total} sheets fine`),
        el("table", {}, el("tr", {}, el("th", {}, "Sample"), el("th", {}, "Blocks not found")), rows)
      );
    } catch (error) {
      box.replaceChildren(el("div", { class: "error" }, error.message));
    }
  }
}
