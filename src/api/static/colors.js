// Colours panel for the template editor: load a sample sheet, see its main
// colours, pick one with the eyedropper and tune the template's colorDropout
// with a live before/after preview. Plain ES module, no build step (Chrome 109+).
import { api, el, toast } from "./api.js";

const MODES = [
  ["grey", "Grey (no colour removal)"],
  ["red", "Red channel: removes red / pink / orange print"],
  ["green", "Green channel"],
  ["blue", "Blue channel: removes blue / cyan print"],
  ["max", "Brightest channel: removes every saturated colour (blue pen too)"],
  ["color", "One colour: removes colours close to the picked one"],
];
const PREVIEW_WIDTH = 900;

function ensureStyles() {
  if (document.getElementById("colors-css")) return;
  const link = document.createElement("link");
  link.id = "colors-css";
  link.rel = "stylesheet";
  link.href = "/static/colors.css";
  document.head.append(link);
}

function hexOf(r, g, b) {
  return "#" + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("").toUpperCase();
}

function normalizeHex(text) {
  const m = /^#?([0-9a-f]{6}|[0-9a-f]{3})$/i.exec((text || "").trim());
  if (!m) return null;
  let h = m[1];
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  return "#" + h.toUpperCase();
}

// Settings object as stored in template.json ("grey" removes the key)
export function dropoutFromControls(c) {
  if (c.mode === "grey") return null;
  const out = { mode: c.mode, strength: Math.round(c.strength * 100) / 100 };
  if (c.mode === "color") {
    out.color = c.color;
    out.tolerance = Math.round(c.tolerance);
  }
  return out;
}

export function controlsFromDropout(value) {
  const c = { mode: "grey", color: "#E8618C", tolerance: 60, strength: 1 };
  if (!value) return c;
  if (typeof value === "string") return { ...c, mode: value === "gray" ? "grey" : value };
  return {
    mode: value.mode === "gray" ? "grey" : value.mode || "grey",
    color: normalizeHex(value.color) || c.color,
    tolerance: value.tolerance ?? c.tolerance,
    strength: value.strength ?? 1,
  };
}

export class ColourPanel {
  // editor: the TemplateEditor (doc, edit(), id)
  constructor(editor) {
    this.editor = editor;
    this.controls = controlsFromDropout(editor.doc.colorDropout);
    this.blob = null;
    this.timer = null;
    this.previewUrl = null;
    this.seq = 0;
    ensureStyles();
  }

  open() {
    const fileInput = el("input", { type: "file", accept: "image/*,.pdf", onchange: (e) => this.load(e.target.files[0]) });
    this.sourceCanvas = el("canvas", { class: "co-canvas", title: "Click to pick a colour to remove" });
    this.sourceCanvas.addEventListener("click", (e) => this.pick(e));
    this.sourceCanvas.addEventListener("mousemove", (e) => this.hover(e));
    this.previewImg = el("img", { class: "co-canvas", alt: "Preview" });
    this.swatches = el("div", { class: "co-swatches" }, el("div", { class: "muted small" }, "Load a sample sheet to see its colours."));
    this.hoverChip = el("span", { class: "co-hover" });
    this.info = el("div", { class: "muted small co-info" });
    this.form = el("div", { class: "co-controls" });
    this.renderControls();
    this.root = el(
      "div",
      { class: "co-overlay", onclick: (e) => e.target === this.root && this.close() },
      el(
        "div",
        { class: "co-dialog" },
        el(
          "div",
          { class: "co-head" },
          el("h3", {}, "Colours"),
          el("label", { class: "small" }, "Sample sheet ", fileInput),
          this.hoverChip,
          el("span", { class: "co-grow" }),
          el("button", { class: "small primary", onclick: () => this.apply() }, "Use for template"),
          el("button", { class: "small ghost", onclick: () => this.close() }, "Close")
        ),
        el(
          "div",
          { class: "co-body" },
          el(
            "div",
            { class: "co-side" },
            el("h4", {}, "Main colours"),
            this.swatches,
            el("h4", {}, "Colour removal"),
            this.form,
            this.info
          ),
          el(
            "div",
            { class: "co-views" },
            el("figure", {}, el("figcaption", {}, "Before (click to pick a colour)"), this.sourceCanvas),
            el("figure", {}, el("figcaption", {}, "After: what the reader sees"), this.previewImg)
          )
        )
      )
    );
    this.keyHandler = (e) => e.key === "Escape" && this.close();
    document.addEventListener("keydown", this.keyHandler);
    document.body.append(this.root);
  }

  close() {
    document.removeEventListener("keydown", this.keyHandler);
    if (this.previewUrl) URL.revokeObjectURL(this.previewUrl);
    this.root.remove();
  }

  renderControls() {
    const c = this.controls;
    const f = this.form;
    f.innerHTML = "";
    const mode = el("select", {}, MODES.map(([v, t]) => el("option", { value: v, selected: v === c.mode }, t)));
    mode.addEventListener("change", () => {
      c.mode = mode.value;
      this.renderControls();
      this.schedule();
    });
    f.append(el("label", { class: "field" }, "Mode", mode));
    if (c.mode === "color") {
      const picker = el("input", { type: "color", value: c.color.toLowerCase() });
      const hex = el("input", { value: c.color, size: 8, class: "co-hex" });
      picker.addEventListener("input", () => {
        c.color = picker.value.toUpperCase();
        hex.value = c.color;
        this.schedule();
      });
      hex.addEventListener("change", () => {
        const v = normalizeHex(hex.value);
        if (!v) return toast("Use a hex colour like #E8618C", "error");
        c.color = v;
        picker.value = v.toLowerCase();
        this.schedule();
      });
      f.append(el("label", { class: "field" }, "Colour to remove", el("div", { class: "row gap" }, picker, hex)));
      f.append(this.slider("Tolerance", c.tolerance, 5, 150, 1, (v) => (c.tolerance = v)));
    }
    if (c.mode !== "grey") f.append(this.slider("Strength", c.strength, 0, 1, 0.05, (v) => (c.strength = v)));
    const current = this.editor.doc.colorDropout;
    f.append(el("div", { class: "muted small" }, "Template now: ", el("code", {}, current ? JSON.stringify(current) : "grey (default)")));
  }

  slider(label, value, min, max, step, onchange) {
    const out = el("span", { class: "co-value" }, String(value));
    const input = el("input", { type: "range", min, max, step, value });
    input.addEventListener("input", () => {
      const v = Number(input.value);
      out.textContent = String(v);
      onchange(v);
      this.schedule();
    });
    return el("label", { class: "field" }, el("span", {}, label, " ", out), input);
  }

  // ------------------------------------------------------------------ sample
  async load(file) {
    if (!file) return;
    try {
      const bitmap = file.type === "application/pdf" || /\.pdf$/i.test(file.name) ? null : await this.decode(file);
      if (bitmap) {
        this.drawSource(bitmap);
        this.blob = await new Promise((resolve) => this.sourceCanvas.toBlob(resolve, "image/png"));
      } else {
        this.blob = file; // the server renders the PDF; the preview is the only view
      }
      this.swatches.innerHTML = "";
      this.swatches.append(el("div", { class: "muted small" }, "Analysing…"));
      const form = new FormData();
      form.append("file", this.blob, bitmap ? "sample.png" : file.name);
      if (this.editor.id) form.append("template_id", this.editor.id);
      const palette = await api("/tools/colors", { method: "POST", form });
      this.renderSwatches(palette);
      this.schedule(0);
    } catch (error) {
      toast(error.message || String(error), "error");
    }
  }

  decode(file) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      const src = URL.createObjectURL(file);
      img.onload = () => {
        URL.revokeObjectURL(src);
        resolve(img);
      };
      img.onerror = () => {
        URL.revokeObjectURL(src);
        reject(new Error("The browser cannot display this image; try a JPEG or PNG"));
      };
      img.src = src;
    });
  }

  drawSource(img) {
    const scale = Math.min(1, PREVIEW_WIDTH / img.naturalWidth);
    const canvas = this.sourceCanvas;
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  }

  renderSwatches(palette) {
    const box = this.swatches;
    box.innerHTML = "";
    const colors = palette.colors || [];
    box.append(el("div", { class: "muted small" }, `Paper ${Math.round((palette.paper_share || 0) * 100)}% of the sheet`));
    if (!colors.length) box.append(el("div", { class: "muted small" }, "No colours besides the paper."));
    for (const c of colors) {
      const s = c.suggestion;
      box.append(
        el(
          "div",
          { class: "co-swatch" },
          el("span", { class: "co-chip", style: { background: c.hex } }),
          el("code", {}, c.hex),
          el("span", { class: "co-share" }, `${(c.share * 100).toFixed(1)}%`),
          el("span", { class: "co-label" }, c.label_guess),
          s
            ? el(
                "button",
                {
                  class: "small",
                  title: s.good ? "Apply the suggested settings" : "Best available, but the contrast to pens is weak",
                  onclick: () => this.useSuggestion(s.settings),
                },
                s.good ? "Remove" : "Remove (weak)"
              )
            : null
        )
      );
    }
  }

  useSuggestion(settings) {
    this.controls = controlsFromDropout(settings);
    this.renderControls();
    this.schedule(0);
  }

  // ------------------------------------------------------------------ eyedropper
  sample(e) {
    const canvas = this.sourceCanvas;
    if (!canvas.width) return null;
    const r = canvas.getBoundingClientRect();
    const x = Math.floor(((e.clientX - r.left) / r.width) * canvas.width);
    const y = Math.floor(((e.clientY - r.top) / r.height) * canvas.height);
    const x0 = Math.max(0, x - 2);
    const y0 = Math.max(0, y - 2);
    const data = canvas.getContext("2d", { willReadFrequently: true }).getImageData(x0, y0, 5, 5).data;
    let rr = 0, gg = 0, bb = 0, n = 0;
    for (let i = 0; i < data.length; i += 4) {
      rr += data[i];
      gg += data[i + 1];
      bb += data[i + 2];
      n++;
    }
    return n ? hexOf(rr / n, gg / n, bb / n) : null;
  }

  hover(e) {
    const hex = this.sample(e);
    this.hoverChip.innerHTML = "";
    if (hex) this.hoverChip.append(el("span", { class: "co-chip", style: { background: hex } }), " ", el("code", {}, hex));
  }

  async pick(e) {
    const hex = this.sample(e);
    if (!hex) return;
    try {
      const s = await api("/tools/dropout-suggest", { method: "POST", json: { target: hex } });
      this.controls = controlsFromDropout(s.settings);
      this.renderControls();
      this.info.textContent = `${hex} → ${s.target_after} (white = 255) with ${s.settings.mode}; pens stay ≤ ${Math.max(...Object.values(s.keep_after))}.${s.good ? "" : " Weak contrast: check marks in the preview."}`;
      this.schedule(0);
    } catch (error) {
      toast(error.message, "error");
    }
  }

  // ------------------------------------------------------------------ preview
  schedule(delay = 250) {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.preview(), delay);
  }

  async preview() {
    if (!this.blob) return;
    const seq = ++this.seq;
    const form = new FormData();
    form.append("file", this.blob, this.blob.name || "sample.png");
    form.append("settings", JSON.stringify(dropoutFromControls(this.controls) || "grey"));
    form.append("max_width", String(PREVIEW_WIDTH));
    try {
      const response = await api("/tools/dropout-preview", { method: "POST", form, raw: true });
      const blob = await response.blob();
      if (seq !== this.seq) return; // a newer request is on its way
      if (this.previewUrl) URL.revokeObjectURL(this.previewUrl);
      this.previewUrl = URL.createObjectURL(blob);
      this.previewImg.src = this.previewUrl;
    } catch (error) {
      toast(error.message, "error");
    }
  }

  apply() {
    const value = dropoutFromControls(this.controls);
    this.editor.edit(() => {
      if (value) this.editor.doc.colorDropout = value;
      else delete this.editor.doc.colorDropout;
    });
    this.renderControls();
    toast(value ? `Colour removal set: ${value.mode}` : "Colour removal off (grey)", "ok");
  }
}
