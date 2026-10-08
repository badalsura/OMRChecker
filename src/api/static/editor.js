// Template editor: draws the template over a reference image and lets the user
// move / resize / add / delete field blocks and zones with snapping, undo and zoom.
import { api, el, errorList, state, toast, url } from "./api.js";
import { ColourPanel } from "./colors.js";
import { alignmentSection, blockAlignmentFields } from "./editor_align.js";
import { ocrZoneControls } from "./editor_ocr.js";
import { openJsonEditor } from "./editor_json.js";
import { openScoring } from "./scoring.js";
import { renameTemplate } from "./template_ops.js";
import { alignmentPick, drawAlignment, renderAlignmentPanel } from "./editor_tracks.js";
import { renderWarnings } from "./generator_warnings.js";

const ZONE_COLORS = { barcode: "#d9661a", qrcode: "#a03ca0", ocr: "#1e8c1e", icr: "#1478dc", image: "#787878" };
const BLOCK_COLOR = "#2f6fdf";
const HIGHLIGHT = "#ff8a00";
const HANDLE = 8;
const SNAP_PX = 7; // screen pixels
const REPORT_KEYS = ["needs_verification", "verify", "verification", "items", "flagged", "review", "needs_review", "uncertain", "low_confidence", "issues"];

export function parseFieldString(s) {
  const m = /^([^.\d]+)(\d+)\.{2,3}(\d+)$/.exec(s);
  if (!m) return [s];
  const out = [];
  for (let i = Number(m[2]); i <= Number(m[3]); i++) out.push(`${m[1]}${i}`);
  return out;
}

const clone = (v) => JSON.parse(JSON.stringify(v));
const FULL = "\u0000files:"; // undo entry covering template + config + evaluation
const round1 = (v) => Math.round(v * 10) / 10;

export class TemplateEditor {
  constructor(root, { onClose }) {
    this.root = root;
    this.onClose = onClose;
    this.view = { scale: 0.5, ox: 20, oy: 20 };
    this.grid = 5;
    this.snapEdges = true;
    this.mode = null;
    this.selected = null;
    this.undo = [];
    this.redo = [];
    this.guides = [];
    this.bg = null;
    this.testResult = null;
    this.errors = [];
    this.keyHandler = (e) => this.onKey(e);
    this.resolved = new Set();
  }

  // ------------------------------------------------------------------ open
  async open(templateId) {
    const detail = await api(`/templates/${templateId}`);
    this.detail = detail;
    this.id = templateId;
    this.doc = clone(detail.template);
    this.doc.fieldBlocks = this.doc.fieldBlocks || {};
    this.doc.zones = this.doc.zones || {};
    this.configText = detail.config ? JSON.stringify(detail.config, null, 2) : "";
    this.evaluationText = detail.evaluation ? JSON.stringify(detail.evaluation, null, 2) : "";
    this.savedConfigText = this.configText;
    this.savedEvaluationText = this.evaluationText;
    this.undo = [];
    this.redo = [];
    this.selected = null;
    this.dirty = false;
    this.testResult = null;
    this.errors = detail.validation_errors || [];
    this.report = detail.report_confirmed ? null : this.parseReport(detail.report);
    this.build();
    this.bgMode = detail.has_reference ? "reference" : "blank";
    await this.loadBackground();
    this.fit();
    document.addEventListener("keydown", this.keyHandler);
  }

  close() {
    if (this.dirty && !confirm("Discard unsaved changes?")) return;
    document.removeEventListener("keydown", this.keyHandler);
    if (this.resizeObserver) this.resizeObserver.disconnect();
    this.root.innerHTML = "";
    this.onClose();
  }

  // ------------------------------------------------------------------ layout
  build() {
    const btn = (label, title, fn, cls = "small") => el("button", { class: cls, title, onclick: fn }, label);
    this.saveBtn = btn("Save", "Save (Ctrl+S)", () => this.save(), "small primary");
    this.undoBtn = btn("Undo", "Undo (Ctrl+Z)", () => this.doUndo());
    this.redoBtn = btn("Redo", "Redo (Ctrl+Shift+Z)", () => this.doRedo());
    this.addBlockBtn = btn("+ Block", "Draw a new bubble block (B)", () => this.setMode("add-block"));
    this.addZoneBtn = btn("+ Zone", "Draw a new barcode / QR / OCR / ICR zone (Z)", () => this.setMode("add-zone"));
    this.zoneTypeSel = el("select", { class: "small", title: "Type of new zones" }, (state.caps?.zone_types || ["barcode", "qrcode", "ocr", "icr"]).map((t) => el("option", { value: t }, t)));
    const gridSel = el(
      "select",
      { class: "small", title: "Snap grid", onchange: (e) => (this.grid = Number(e.target.value)) },
      [1, 2, 5, 10, 20].map((g) => el("option", { value: g, selected: g === this.grid }, `grid ${g}px`))
    );
    const edgeChk = el("input", { type: "checkbox", checked: true, onchange: (e) => (this.snapEdges = e.target.checked) });
    this.bgSel = el(
      "select",
      { class: "small", title: "Background", onchange: (e) => this.changeBackground(e.target.value) },
      el("option", { value: "reference" }, "Reference image"),
      el("option", { value: "scan" }, "Last test scan"),
      el("option", { value: "blank" }, "Blank page"),
      el("option", { value: "upload" }, "Upload background…")
    );
    this.testInput = el("input", { type: "file", accept: "image/*,.pdf", hidden: true, onchange: (e) => this.testRead(e.target.files[0]) });
    this.bgInput = el("input", { type: "file", accept: "image/*", hidden: true, onchange: (e) => this.uploadBackground(e.target.files[0]) });
    this.titleEl = el("span", { class: "title editable-title", title: "Click to rename (jobs and results stay linked)", tabindex: "0", onclick: () => this.rename() }, this.detail.name);
    const toolbar = el(
      "div",
      { class: "ed-toolbar" },
      btn("← Back", "Back to templates", () => this.close(), "small ghost"),
      this.titleEl,
      (this.statusChip = el("span", { class: `chip ${this.detail.status}` }, this.detail.status)),
      el("span", { class: "sep" }),
      this.saveBtn,
      this.undoBtn,
      this.redoBtn,
      el("span", { class: "sep" }),
      this.addBlockBtn,
      this.addZoneBtn,
      this.zoneTypeSel,
      btn("Duplicate", "Duplicate selection (Ctrl+D)", () => this.duplicate()),
      btn("Delete", "Delete selection (Del)", () => this.deleteSelected(), "small danger"),
      el("span", { class: "sep" }),
      btn("−", "Zoom out (-)", () => this.zoom(1 / 1.25)),
      btn("Fit", "Fit page (F)", () => this.fit()),
      btn("+", "Zoom in (+)", () => this.zoom(1.25)),
      gridSel,
      el("label", { class: "small muted", title: "Snap to other blocks' edges" }, edgeChk, " edges"),
      el("span", { class: "sep" }),
      this.bgSel,
      btn("Colours…", "Sheet colours and colour removal (colorDropout)", () => new ColourPanel(this).open()),
      btn("Edit JSON", "Edit template.json, config.json and evaluation.json directly", () => openJsonEditor(this)),
      btn("Scoring…", "Answer key and marking scheme (evaluation.json)", () => openScoring(this)),
      btn("Test read…", "Save, read a sample sheet with this template and overlay the result", () => this.testInput.click()),
      this.testInput,
      this.bgInput
    );
    this.canvas = el("canvas", { tabindex: "0" });
    this.status = el("div", { class: "ed-status" }, "");
    this.wrap = el("div", { class: "ed-canvas-wrap" }, this.canvas, this.status);
    this.side = el("div", { class: "ed-side" });
    this.root.innerHTML = "";
    this.root.append(toolbar, el("div", { class: "ed-body" }, this.wrap, this.side));
    this.ctx = this.canvas.getContext("2d");
    this.resizeObserver = new ResizeObserver(() => this.draw());
    this.resizeObserver.observe(this.wrap);
    this.bindCanvas();
    this.renderSide();
    this.updateButtons();
  }

  // ------------------------------------------------------------------ geometry
  fieldTypes() {
    return state.caps?.field_types || {};
  }

  blockInfo(name) {
    const raw = this.doc.fieldBlocks[name];
    const merged = { ...(this.fieldTypes()[raw.fieldType] || {}), ...raw };
    const [bw, bh] = merged.bubbleDimensions || this.doc.bubbleDimensions || [20, 20];
    const values = merged.bubbleValues || [];
    const labels = (merged.fieldLabels || []).flatMap(parseFieldString);
    const horizontal = merged.direction === "horizontal";
    const bg = Number(merged.bubblesGap) || 0;
    const lg = Number(merged.labelsGap) || 0;
    const nV = Math.max(values.length, 1);
    const nF = Math.max(labels.length, 1);
    const [x, y] = merged.origin || [0, 0];
    const w = horizontal ? bg * (nV - 1) + bw : lg * (nF - 1) + bw;
    const h = horizontal ? lg * (nF - 1) + bh : bg * (nV - 1) + bh;
    return { kind: "block", name, x, y, w, h, bw, bh, values, labels, horizontal, bg, lg, nV, nF, raw };
  }

  zoneInfo(name) {
    const raw = this.doc.zones[name];
    const [x, y] = raw.origin || [0, 0];
    const [w, h] = raw.dimensions || [10, 10];
    return { kind: "zone", name, x, y, w, h, raw };
  }

  objects() {
    return [
      ...Object.keys(this.doc.fieldBlocks).map((n) => this.blockInfo(n)),
      ...Object.keys(this.doc.zones).map((n) => this.zoneInfo(n)),
    ];
  }

  info(sel) {
    if (!sel) return null;
    if (sel.kind === "block" && this.doc.fieldBlocks[sel.name]) return this.blockInfo(sel.name);
    if (sel.kind === "zone" && this.doc.zones[sel.name]) return this.zoneInfo(sel.name);
    return null;
  }

  page() {
    return this.doc.pageDimensions || [1000, 1400];
  }

  // ------------------------------------------------------------------ view
  toPage(e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: (e.clientX - r.left - this.view.ox) / this.view.scale, y: (e.clientY - r.top - this.view.oy) / this.view.scale };
  }

  fit() {
    const r = this.wrap.getBoundingClientRect();
    const [pw, ph] = this.page();
    const scale = Math.min((r.width - 40) / pw, (r.height - 40) / ph) || 0.5;
    this.view = { scale, ox: (r.width - pw * scale) / 2, oy: (r.height - ph * scale) / 2 };
    this.draw();
  }

  zoom(factor, cx, cy) {
    const r = this.wrap.getBoundingClientRect();
    cx = cx ?? r.width / 2;
    cy = cy ?? r.height / 2;
    const scale = Math.min(Math.max(this.view.scale * factor, 0.05), 8);
    const k = scale / this.view.scale;
    this.view = { scale, ox: cx - (cx - this.view.ox) * k, oy: cy - (cy - this.view.oy) * k };
    this.draw();
  }

  // ------------------------------------------------------------------ background
  async loadBackground() {
    this.bgSel.value = this.bgMode;
    let src = null;
    if (this.bgMode === "reference" && this.detail.reference_url) src = url(`${this.detail.reference_url}?t=${Date.now()}`);
    if (this.bgMode === "scan" && this.testResult?.links?.aligned) src = url(this.testResult.links.aligned);
    if (!src) {
      this.bg = null;
      this.draw();
      return;
    }
    await new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        this.bg = img;
        resolve();
      };
      img.onerror = () => {
        this.bg = null;
        resolve();
      };
      img.src = src;
    });
    this.draw();
  }

  changeBackground(mode) {
    if (mode === "upload") {
      this.bgSel.value = this.bgMode;
      this.bgInput.click();
      return;
    }
    if (mode === "reference" && !this.detail.reference_url) {
      toast("No reference image: upload one or use a test scan");
      this.bgSel.value = this.bgMode;
      return;
    }
    if (mode === "scan" && !this.testResult) {
      toast("Run 'Test read…' first");
      this.bgSel.value = this.bgMode;
      return;
    }
    this.bgMode = mode;
    this.loadBackground();
  }

  async uploadBackground(file) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      this.detail = { ...this.detail, ...(await api(`/templates/${this.id}/reference`, { method: "POST", form })) };
      this.bgMode = "reference";
      await this.loadBackground();
      toast("Background saved as the template reference image", "ok");
    } catch (error) {
      toast(error.message, "error");
    }
    this.bgInput.value = "";
  }

  // ------------------------------------------------------------------ drawing
  draw() {
    if (!this.ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const r = this.wrap.getBoundingClientRect();
    if (this.canvas.width !== Math.round(r.width * dpr) || this.canvas.height !== Math.round(r.height * dpr)) {
      this.canvas.width = Math.round(r.width * dpr);
      this.canvas.height = Math.round(r.height * dpr);
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, r.width, r.height);
    const { scale, ox, oy } = this.view;
    const [pw, ph] = this.page();
    ctx.save();
    ctx.translate(ox, oy);
    ctx.scale(scale, scale);
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, pw, ph);
    if (this.bg) {
      ctx.imageSmoothingEnabled = scale < 1;
      ctx.drawImage(this.bg, 0, 0, pw, ph);
    }
    ctx.restore();

    const S = (v) => v * scale;
    const X = (v) => ox + v * scale;
    const Y = (v) => oy + v * scale;
    const highlighted = this.highlightNames();
    const overlay = this.testResult && this.testOverlay;
    const testFields = (overlay && this.testResult.fields) || {};
    const testZones = (overlay && this.testResult.zones) || {};

    for (const o of this.objects()) {
      const isSel = this.selected && this.selected.kind === o.kind && this.selected.name === o.name;
      const hl = highlighted.has(o.name);
      if (o.kind === "block") {
        ctx.lineWidth = 1;
        for (let f = 0; f < o.nF; f++) {
          const label = o.labels[f];
          const tf = testFields[label];
          for (let v = 0; v < o.nV; v++) {
            const bx = o.horizontal ? o.x + v * o.bg : o.x + f * o.lg;
            const by = o.horizontal ? o.y + f * o.lg : o.y + v * o.bg;
            const bubble = tf?.bubbles?.[v];
            if (bubble?.marked) {
              ctx.fillStyle = "rgba(31,157,85,0.45)";
              ctx.fillRect(X(bx), Y(by), S(o.bw), S(o.bh));
            }
            ctx.strokeStyle = tf?.needs_review ? "#d0342c" : "rgba(31,157,85,0.95)";
            ctx.strokeRect(X(bx) + 0.5, Y(by) + 0.5, S(o.bw), S(o.bh));
          }
          if (tf && scale > 0.35) {
            ctx.fillStyle = tf.needs_review ? "#d0342c" : "#1f6f3f";
            ctx.font = `${Math.max(10, S(o.bh) * 0.6)}px ui-monospace, monospace`;
            const lx = o.horizontal ? o.x + o.w + 6 : o.x + f * o.lg;
            const ly = o.horizontal ? o.y + f * o.lg + o.bh * 0.8 : o.y + o.h + o.bh * 0.8;
            ctx.fillText(tf.value === "" ? "·" : tf.value, X(lx), Y(ly));
          }
        }
        this.strokeBox(o, isSel ? BLOCK_COLOR : hl ? HIGHLIGHT : "rgba(47,111,223,0.8)", isSel ? 2 : hl ? 2.5 : 1.25, hl && !isSel);
        this.label(o, `${o.name} · ${o.labels[0] || ""}${o.nF > 1 ? "…" + o.labels[o.nF - 1] : ""}`, hl ? HIGHLIGHT : BLOCK_COLOR);
      } else {
        const color = ZONE_COLORS[o.raw.type] || "#333";
        ctx.fillStyle = color + "18";
        ctx.fillRect(X(o.x), Y(o.y), S(o.w), S(o.h));
        this.strokeBox(o, hl && !isSel ? HIGHLIGHT : color, isSel ? 2.5 : hl ? 2.5 : 1.5, hl && !isSel);
        const tz = testZones[o.name];
        this.label(o, `${o.name} (${o.raw.type})${tz ? " = " + (tz.value || "∅") : ""}`, tz?.needs_review ? "#d0342c" : color);
      }
      if (isSel) this.drawHandles(o);
    }
    drawAlignment(this); // timing tracks and index points (editor_tracks.js)
    // alignment guides
    ctx.strokeStyle = "#ff2d95";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    for (const g of this.guides) {
      ctx.beginPath();
      if (g.axis === "x") {
        ctx.moveTo(X(g.at) + 0.5, Y(0));
        ctx.lineTo(X(g.at) + 0.5, Y(ph));
      } else {
        ctx.moveTo(X(0), Y(g.at) + 0.5);
        ctx.lineTo(X(pw), Y(g.at) + 0.5);
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);
    if (this.rubber) {
      const b = this.rubber;
      ctx.strokeStyle = "#ff2d95";
      ctx.setLineDash([5, 4]);
      ctx.strokeRect(X(Math.min(b.x0, b.x1)), Y(Math.min(b.y0, b.y1)), S(Math.abs(b.x1 - b.x0)), S(Math.abs(b.y1 - b.y0)));
      ctx.setLineDash([]);
    }
  }

  strokeBox(o, color, width, dashed) {
    const { scale, ox, oy } = this.view;
    const ctx = this.ctx;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    if (dashed) ctx.setLineDash([6, 4]);
    const pad = o.kind === "block" ? 3 : 0;
    ctx.strokeRect(ox + (o.x - pad) * scale, oy + (o.y - pad) * scale, (o.w + 2 * pad) * scale, (o.h + 2 * pad) * scale);
    ctx.setLineDash([]);
  }

  label(o, text, color) {
    const { scale, ox, oy } = this.view;
    const ctx = this.ctx;
    ctx.font = "12px system-ui, sans-serif";
    const w = ctx.measureText(text).width + 8;
    const x = ox + o.x * scale;
    const y = oy + o.y * scale - 18;
    ctx.fillStyle = "rgba(255,255,255,0.88)";
    ctx.fillRect(x, y, w, 16);
    ctx.fillStyle = color;
    ctx.fillText(text, x + 4, y + 12);
  }

  handles(o) {
    const { x, y, w, h } = o;
    return {
      nw: [x, y], n: [x + w / 2, y], ne: [x + w, y], e: [x + w, y + h / 2],
      se: [x + w, y + h], s: [x + w / 2, y + h], sw: [x, y + h], w: [x, y + h / 2],
    };
  }

  drawHandles(o) {
    const { scale, ox, oy } = this.view;
    const ctx = this.ctx;
    for (const [px, py] of Object.values(this.handles(o))) {
      ctx.fillStyle = "#fff";
      ctx.strokeStyle = BLOCK_COLOR;
      ctx.lineWidth = 1.5;
      ctx.fillRect(ox + px * scale - HANDLE / 2, oy + py * scale - HANDLE / 2, HANDLE, HANDLE);
      ctx.strokeRect(ox + px * scale - HANDLE / 2, oy + py * scale - HANDLE / 2, HANDLE, HANDLE);
    }
  }

  // ------------------------------------------------------------------ interaction
  hitHandle(p) {
    const o = this.info(this.selected);
    if (!o) return null;
    const tol = (HANDLE / 2 + 3) / this.view.scale;
    for (const [name, [hx, hy]] of Object.entries(this.handles(o))) {
      if (Math.abs(p.x - hx) <= tol && Math.abs(p.y - hy) <= tol) return name;
    }
    return null;
  }

  hitObject(p) {
    const tol = 4 / this.view.scale;
    let best = null;
    for (const o of this.objects()) {
      if (p.x >= o.x - tol && p.x <= o.x + o.w + tol && p.y >= o.y - tol && p.y <= o.y + o.h + tol) {
        const area = o.w * o.h;
        if (!best || area < best.area) best = { o, area };
      }
    }
    return best?.o || null;
  }

  bindCanvas() {
    const c = this.canvas;
    c.addEventListener("contextmenu", (e) => e.preventDefault());
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      if (e.shiftKey) {
        this.view.ox -= e.deltaY;
        this.draw();
      } else {
        this.zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - r.left, e.clientY - r.top);
      }
    }, { passive: false });
    c.addEventListener("mousedown", (e) => this.onDown(e));
    window.addEventListener("mousemove", (e) => this.onMove(e));
    window.addEventListener("mouseup", (e) => this.onUp(e));
    c.addEventListener("dblclick", (e) => {
      const o = this.hitObject(this.toPage(e));
      if (o) {
        this.select(o);
        const first = this.side.querySelector("input");
        if (first) first.focus();
      }
    });
  }

  onDown(e) {
    if (!this.doc) return;
    this.canvas.focus();
    const p = this.toPage(e);
    if (e.button === 1 || e.button === 2 || this.spaceDown) {
      this.drag = { type: "pan", sx: e.clientX, sy: e.clientY, ox: this.view.ox, oy: this.view.oy };
      return;
    }
    if (this.mode) {
      this.rubber = { x0: this.snapValue(p.x), y0: this.snapValue(p.y), x1: p.x, y1: p.y };
      this.drag = { type: "create" };
      return;
    }
    const handle = this.hitHandle(p);
    if (handle) {
      const o = this.info(this.selected);
      this.drag = { type: "resize", handle, start: p, orig: { ...o }, snapshot: JSON.stringify(this.doc) };
      return;
    }
    const o = this.hitObject(p);
    if (o) {
      this.select(o);
      this.drag = { type: "move", start: p, orig: { x: o.x, y: o.y }, snapshot: JSON.stringify(this.doc) };
    } else {
      this.select(null);
      this.drag = { type: "pan", sx: e.clientX, sy: e.clientY, ox: this.view.ox, oy: this.view.oy };
    }
  }

  onMove(e) {
    if (!this.doc || !this.canvas.isConnected) return;
    const p = this.toPage(e);
    const inside = e.target === this.canvas;
    if (inside) {
      const hover = this.hitObject(p);
      this.status.textContent = `x ${Math.round(p.x)}  y ${Math.round(p.y)}  zoom ${Math.round(this.view.scale * 100)}%${hover ? "  · " + hover.name : ""}${this.mode ? "  · drag to draw" : ""}`;
      if (!this.drag) {
        const handle = this.hitHandle(p);
        const cursors = { nw: "nwse-resize", se: "nwse-resize", ne: "nesw-resize", sw: "nesw-resize", n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize" };
        this.canvas.style.cursor = this.mode ? "crosshair" : handle ? cursors[handle] : hover ? "move" : "grab";
      }
    }
    const d = this.drag;
    if (!d) return;
    if (d.type === "pan") {
      this.view.ox = d.ox + (e.clientX - d.sx);
      this.view.oy = d.oy + (e.clientY - d.sy);
      this.canvas.style.cursor = "grabbing";
      this.draw();
    } else if (d.type === "create") {
      this.rubber.x1 = this.snapValue(p.x);
      this.rubber.y1 = this.snapValue(p.y);
      this.draw();
    } else if (d.type === "move") {
      const o = this.info(this.selected);
      let x = this.snapValue(d.orig.x + (p.x - d.start.x));
      let y = this.snapValue(d.orig.y + (p.y - d.start.y));
      [x, y] = this.snapEdgesMove({ ...o, x, y });
      this.setOrigin(this.selected, Math.round(x), Math.round(y));
      d.moved = true;
      this.draw();
    } else if (d.type === "resize") {
      this.resizeTo(d, p);
      d.moved = true;
      this.draw();
    }
  }

  onUp() {
    const d = this.drag;
    if (!d) return;
    this.drag = null;
    this.guides = [];
    if (d.type === "create") {
      const b = this.rubber;
      this.rubber = null;
      const rect = { x: Math.round(Math.min(b.x0, b.x1)), y: Math.round(Math.min(b.y0, b.y1)), w: Math.round(Math.abs(b.x1 - b.x0)), h: Math.round(Math.abs(b.y1 - b.y0)) };
      if (this.mode && this.mode.startsWith("align")) {
        alignmentPick(this, rect); // Alignment panel tools (editor_tracks.js)
        this.draw();
        return;
      }
      if (rect.w > 4 && rect.h > 4) {
        if (this.mode === "add-block") this.createBlock(rect);
        else this.createZone(rect);
      }
      this.setMode(null);
    } else if ((d.type === "move" || d.type === "resize") && d.moved) {
      this.pushUndo(d.snapshot);
      this.changed();
    }
    this.draw();
  }

  snapValue(v) {
    return this.grid > 1 ? Math.round(v / this.grid) * this.grid : Math.round(v);
  }

  // Snap a moving rectangle's edges / centre to other objects
  snapEdgesMove(o) {
    this.guides = [];
    if (!this.snapEdges) return [o.x, o.y];
    const tol = SNAP_PX / this.view.scale;
    const others = this.objects().filter((x) => !(x.kind === o.kind && x.name === o.name));
    const best = (mine, theirs) => {
      let found = null;
      for (const m of mine) for (const t of theirs) {
        const diff = t - m;
        if (Math.abs(diff) <= tol && (!found || Math.abs(diff) < Math.abs(found.diff))) found = { diff, at: t };
      }
      return found;
    };
    const sx = best([o.x, o.x + o.w, o.x + o.w / 2], others.flatMap((t) => [t.x, t.x + t.w, t.x + t.w / 2]));
    const sy = best([o.y, o.y + o.h, o.y + o.h / 2], others.flatMap((t) => [t.y, t.y + t.h, t.y + t.h / 2]));
    let { x, y } = o;
    if (sx) {
      x += sx.diff;
      this.guides.push({ axis: "x", at: sx.at });
    }
    if (sy) {
      y += sy.diff;
      this.guides.push({ axis: "y", at: sy.at });
    }
    return [x, y];
  }

  snapEdge(value, axis, selfName) {
    if (!this.snapEdges) return value;
    const tol = SNAP_PX / this.view.scale;
    let best = null;
    for (const t of this.objects()) {
      if (t.name === selfName) continue;
      const cands = axis === "x" ? [t.x, t.x + t.w] : [t.y, t.y + t.h];
      for (const c of cands) if (Math.abs(c - value) <= tol && (best === null || Math.abs(c - value) < Math.abs(best - value))) best = c;
    }
    if (best !== null) {
      this.guides.push({ axis, at: best });
      return best;
    }
    return value;
  }

  resizeTo(d, p) {
    this.guides = [];
    const o = d.orig;
    let x0 = o.x, y0 = o.y, x1 = o.x + o.w, y1 = o.y + o.h;
    const h = d.handle;
    if (h.includes("w")) x0 = this.snapEdge(this.snapValue(p.x), "x", o.name);
    if (h.includes("e")) x1 = this.snapEdge(this.snapValue(p.x), "x", o.name);
    if (h.includes("n")) y0 = this.snapEdge(this.snapValue(p.y), "y", o.name);
    if (h.includes("s")) y1 = this.snapEdge(this.snapValue(p.y), "y", o.name);
    const min = o.kind === "block" ? 2 : 6;
    if (x1 - x0 < min) h.includes("w") ? (x0 = x1 - min) : (x1 = x0 + min);
    if (y1 - y0 < min) h.includes("n") ? (y0 = y1 - min) : (y1 = y0 + min);
    x0 = Math.max(0, Math.round(x0));
    y0 = Math.max(0, Math.round(y0));
    const w = Math.round(x1) - x0;
    const hh = Math.round(y1) - y0;
    if (o.kind === "zone") {
      const z = this.doc.zones[o.name];
      z.origin = [x0, y0];
      z.dimensions = [w, hh];
    } else {
      // Resizing a block stretches its bubble grid (gaps), bubbles keep their size
      const raw = this.doc.fieldBlocks[o.name];
      raw.origin = [x0, y0];
      const valuesSpan = o.horizontal ? w - o.bw : hh - o.bh;
      const fieldsSpan = o.horizontal ? hh - o.bh : w - o.bw;
      if (o.nV > 1) raw.bubblesGap = Math.max(1, round1(valuesSpan / (o.nV - 1)));
      if (o.nF > 1) raw.labelsGap = Math.max(1, round1(fieldsSpan / (o.nF - 1)));
    }
  }

  setOrigin(sel, x, y) {
    const target = sel.kind === "block" ? this.doc.fieldBlocks[sel.name] : this.doc.zones[sel.name];
    target.origin = [Math.max(0, Math.round(x)), Math.max(0, Math.round(y))];
  }

  select(o) {
    this.selected = o ? { kind: o.kind, name: o.name } : null;
    this.renderSide();
    this.draw();
  }

  setMode(mode) {
    this.mode = this.mode === mode ? null : mode;
    this.addBlockBtn.classList.toggle("active", this.mode === "add-block");
    this.addZoneBtn.classList.toggle("active", this.mode === "add-zone");
    this.canvas.style.cursor = this.mode ? "crosshair" : "default";
  }

  // ------------------------------------------------------------------ edits
  pushUndo(snapshot) {
    this.undo.push(snapshot ?? JSON.stringify(this.doc));
    if (this.undo.length > 200) this.undo.shift();
    this.redo = [];
    this.lastNudge = null;
  }

  edit(fn) {
    const snapshot = JSON.stringify(this.doc);
    fn();
    if (JSON.stringify(this.doc) !== snapshot) {
      this.pushUndo(snapshot);
      this.changed();
    }
    this.renderSide();
    this.draw();
  }

  changed() {
    this.dirty = true;
    // The test overlay shows the saved layout; hide it once the layout changes
    this.testOverlay = false;
    this.updateButtons();
  }

  updateButtons() {
    this.undoBtn.disabled = !this.undo.length;
    this.redoBtn.disabled = !this.redo.length;
    this.saveBtn.textContent = this.dirty ? "Save •" : "Save";
  }

  doUndo() {
    if (!this.undo.length) return;
    this.takeEntry(this.undo.pop(), this.redo);
    this.afterHistory();
  }

  doRedo() {
    if (!this.redo.length) return;
    this.takeEntry(this.redo.pop(), this.undo);
    this.afterHistory();
  }

  // Restore an undo entry, pushing the current state (same kind) on the other stack
  takeEntry(entry, other) {
    if (!entry.startsWith(FULL)) {
      other.push(JSON.stringify(this.doc));
      this.doc = JSON.parse(entry);
      return;
    }
    other.push(FULL + JSON.stringify({ doc: this.doc, config: this.configText, evaluation: this.evaluationText }));
    const s = JSON.parse(entry.slice(FULL.length));
    this.doc = s.doc;
    this.configText = s.config;
    this.evaluationText = s.evaluation;
  }

  // Whole-file edits (Edit JSON, Scoring) as one undo step: {template?, configText?, evaluationText?}
  applyFiles(files) {
    this.pushUndo(FULL + JSON.stringify({ doc: this.doc, config: this.configText, evaluation: this.evaluationText }));
    if (files.template !== undefined) {
      this.doc = clone(files.template);
      this.doc.fieldBlocks = this.doc.fieldBlocks || {};
      this.doc.zones = this.doc.zones || {};
    }
    if (files.configText !== undefined) this.configText = files.configText;
    if (files.evaluationText !== undefined) this.evaluationText = files.evaluationText;
    this.changed();
    this.afterHistory();
  }

  async rename() {
    const name = await renameTemplate({ id: this.id, name: this.detail.name });
    if (!name) return;
    this.detail.name = name;
    this.titleEl.textContent = name;
  }

  async confirmWarnings() {
    if (!confirm("Mark all generator warnings and verification items as checked? They won't be shown again for this layout.")) return;
    try {
      const detail = await api(`/templates/${this.id}/confirm-warnings`, { method: "POST" });
      this.detail.report_confirmed = detail.report_confirmed;
      this.report = null;
      this.renderSide();
      this.draw();
      toast("Warnings cleared", "ok");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  afterHistory() {
    if (this.selected && !this.info(this.selected)) this.selected = null;
    this.dirty = true;
    this.lastNudge = null;
    this.updateButtons();
    this.renderSide();
    this.draw();
  }

  uniqueName(base, dict) {
    let i = 1;
    let name = base;
    while (dict[name] !== undefined) name = `${base}_${++i}`;
    return name;
  }

  allLabels() {
    return new Set(Object.keys(this.doc.fieldBlocks).flatMap((n) => this.blockInfo(n).labels));
  }

  nextNumber(prefix) {
    let max = 0;
    for (const label of this.allLabels()) {
      const m = new RegExp(`^${prefix.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(\\d+)$`).exec(label);
      if (m) max = Math.max(max, Number(m[1]));
    }
    return max + 1;
  }

  createBlock(rect) {
    this.edit(() => {
      const [bw, bh] = this.doc.bubbleDimensions || [20, 20];
      const type = "QTYPE_MCQ4";
      const nV = (this.fieldTypes()[type]?.bubbleValues || ["A", "B", "C", "D"]).length;
      // Guess the number of rows from a typical row pitch of 1.6 bubble heights
      const nF = Math.max(1, Math.round((rect.h - bh) / (bh * 1.6)) + 1);
      const start = this.nextNumber("q");
      const name = this.uniqueName(`MCQ_Block_${Object.keys(this.doc.fieldBlocks).length + 1}`, this.doc.fieldBlocks);
      this.doc.fieldBlocks[name] = {
        fieldType: type,
        origin: [rect.x, rect.y],
        fieldLabels: [nF > 1 ? `q${start}..${start + nF - 1}` : `q${start}`],
        bubblesGap: nV > 1 ? Math.max(1, round1((rect.w - bw) / (nV - 1))) : bw * 1.5,
        labelsGap: nF > 1 ? Math.max(1, round1((rect.h - bh) / (nF - 1))) : bh * 1.6,
      };
      this.selected = { kind: "block", name };
    });
    toast("Block added: set its field type, labels and values in the panel");
  }

  createZone(rect) {
    this.edit(() => {
      const type = this.zoneTypeSel.value;
      const name = this.uniqueName(`${type}_${Object.keys(this.doc.zones).length + 1}`, { ...this.doc.zones, ...Object.fromEntries([...this.allLabels()].map((l) => [l, 1])) });
      this.doc.zones[name] = { type, origin: [rect.x, rect.y], dimensions: [rect.w, rect.h], options: {} };
      this.selected = { kind: "zone", name };
    });
  }

  duplicate() {
    const o = this.info(this.selected);
    if (!o) return;
    this.edit(() => {
      if (o.kind === "zone") {
        const name = this.uniqueName(o.name, this.doc.zones);
        const copy = clone(o.raw);
        copy.origin = [o.x + 20, o.y + o.h + 20];
        this.doc.zones[name] = copy;
        this.selected = { kind: "zone", name };
        return;
      }
      const copy = clone(o.raw);
      // Continue the label numbering, e.g. q1..20 -> q21..40
      copy.fieldLabels = (o.raw.fieldLabels || []).map((s) => {
        const m = /^([^.\d]+)(\d+)(\.{2,3})(\d+)$/.exec(s);
        if (m) {
          const start = this.nextNumber(m[1]);
          return `${m[1]}${start}${m[3]}${start + Number(m[4]) - Number(m[2])}`;
        }
        const single = /^([^.\d]+)(\d+)$/.exec(s);
        if (single) return `${single[1]}${this.nextNumber(single[1])}`;
        return `${s}_2`;
      });
      copy.origin = [Math.round(o.x + o.w + 30), Math.round(o.y)];
      const name = this.uniqueName(o.name.replace(/_\d+$/, "") + "_2", this.doc.fieldBlocks);
      this.doc.fieldBlocks[name] = copy;
      this.selected = { kind: "block", name };
    });
  }

  deleteSelected() {
    const o = this.info(this.selected);
    if (!o) return;
    this.edit(() => {
      if (o.kind === "block") delete this.doc.fieldBlocks[o.name];
      else delete this.doc.zones[o.name];
      this.selected = null;
    });
  }

  nudge(dx, dy) {
    const o = this.info(this.selected);
    if (!o) return;
    const now = Date.now();
    const key = `${o.kind}/${o.name}`;
    if (!this.lastNudge || this.lastNudge.key !== key || now - this.lastNudge.at > 1000) {
      this.pushUndo();
    }
    this.lastNudge = { key, at: now };
    this.setOrigin(this.selected, o.x + dx, o.y + dy);
    this.changed();
    this.renderSide();
    this.draw();
  }

  renameKey(dictName, oldName, newName) {
    const dict = this.doc[dictName];
    const out = {};
    for (const [k, v] of Object.entries(dict)) out[k === oldName ? newName : k] = v;
    this.doc[dictName] = out;
  }

  // ------------------------------------------------------------------ keyboard
  onKey(e) {
    if (!this.root.isConnected || this.root.classList.contains("hidden")) return;
    if (!document.getElementById("tab-templates").classList.contains("active")) return;
    const typing = e.target.matches("input, textarea, select");
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (typing) e.target.blur();
      setTimeout(() => this.save(), 0);
      return;
    }
    if (typing) {
      if (e.key === "Enter" && e.target.tagName === "INPUT") e.target.blur();
      return;
    }
    if (e.key === " ") {
      this.spaceDown = true;
      e.preventDefault();
      window.addEventListener("keyup", () => (this.spaceDown = false), { once: true });
      return;
    }
    if (mod && e.key.toLowerCase() === "z") {
      e.preventDefault();
      e.shiftKey ? this.doRedo() : this.doUndo();
    } else if (mod && e.key.toLowerCase() === "y") {
      e.preventDefault();
      this.doRedo();
    } else if (mod && e.key.toLowerCase() === "d") {
      e.preventDefault();
      this.duplicate();
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      this.deleteSelected();
    } else if (e.key.startsWith("Arrow")) {
      e.preventDefault();
      const step = e.shiftKey ? 10 : 1;
      const delta = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
      this.nudge(...delta);
    } else if (e.key === "Escape") {
      if (this.mode) this.setMode(null);
      else this.select(null);
    } else if (e.key === "Tab") {
      e.preventDefault();
      const list = this.objects();
      if (!list.length) return;
      const i = this.selected ? list.findIndex((o) => o.kind === this.selected.kind && o.name === this.selected.name) : -1;
      this.select(list[(i + (e.shiftKey ? -1 : 1) + list.length) % list.length]);
    } else if (!mod && e.key === "b") this.setMode("add-block");
    else if (!mod && e.key === "z") this.setMode("add-zone");
    else if (!mod && e.key === "f") this.fit();
    else if (e.key === "+" || e.key === "=") this.zoom(1.25);
    else if (e.key === "-") this.zoom(1 / 1.25);
  }

  // ------------------------------------------------------------------ side panel
  renderSide() {
    const side = this.side;
    if (!side) return;
    side.innerHTML = "";
    if (this.errors.length) {
      side.append(el("h3", {}, "Validation errors"), errorList(this.errors));
    }
    const o = this.info(this.selected);
    if (o) side.append(o.kind === "block" ? this.renderBlock(o) : this.renderZone(o));
    if (this.testResult) side.append(this.renderTest());
    if (this.report) side.append(this.renderReport());
    if (!o) side.append(this.renderPage(), renderAlignmentPanel(this));
    side.append(
      el(
        "div",
        { class: "ed-help" },
        el("h3", {}, "Shortcuts"),
        "Drag to move · handles resize (blocks stretch their bubble spacing) · arrows nudge (Shift ×10) · ",
        "Del delete · Ctrl+D duplicate · Ctrl+Z / Ctrl+Shift+Z undo / redo · Ctrl+S save · B / Z draw block / zone · ",
        "wheel zoom · drag empty space, right-drag or Space+drag to pan · Tab cycles selection · F fit"
      )
    );
  }

  input(label, value, onchange, attrs = {}) {
    const input = el("input", { value: value ?? "", ...attrs });
    input.addEventListener("change", () => onchange(input.value, input));
    return el("label", { class: "field" }, label, input);
  }

  num(label, value, onchange, attrs = {}) {
    return this.input(label, value, (v) => {
      if (v === "" && attrs.optional) return onchange(null);
      const n = Number(v);
      if (!Number.isFinite(n) || n < 0) return toast(`${label} must be a positive number`, "error");
      onchange(n);
    }, { type: "number", step: attrs.step || "1", min: "0", placeholder: attrs.placeholder || "" });
  }

  dropdown(label, value, options, onchange) {
    const sel = el("select", {}, options.map(([v, t]) => el("option", { value: v, selected: v === value }, t)));
    sel.addEventListener("change", () => onchange(sel.value));
    return el("label", { class: "field" }, label, sel);
  }

  renderBlock(o) {
    const raw = o.raw;
    const types = Object.keys(this.fieldTypes());
    const custom = !raw.fieldType;
    const set = (fn) => this.edit(fn);
    const box = el("div", {});
    box.append(
      el("h3", {}, "Bubble block"),
      this.input("Name", o.name, (v) => {
        v = v.trim();
        if (!v || v === o.name) return;
        if (this.doc.fieldBlocks[v]) return toast("A block with that name exists", "error");
        set(() => {
          this.renameKey("fieldBlocks", o.name, v);
          this.selected = { kind: "block", name: v };
        });
      }),
      this.dropdown("Field type", raw.fieldType || "", [["", "Custom values"], ...types.map((t) => [t, `${t} (${this.fieldTypes()[t].bubbleValues.join("")})`])], (v) =>
        set(() => {
          if (v) {
            raw.fieldType = v;
            delete raw.bubbleValues;
            delete raw.direction;
          } else {
            const ft = this.fieldTypes()[raw.fieldType] || {};
            raw.bubbleValues = ft.bubbleValues || o.values;
            raw.direction = ft.direction || (o.horizontal ? "horizontal" : "vertical");
            delete raw.fieldType;
          }
        })
      ),
      custom
        ? this.input("Bubble values (comma separated)", o.values.join(","), (v) =>
            set(() => (raw.bubbleValues = v.split(",").map((s) => s.trim()).filter(Boolean)))
          )
        : el("div", { class: "muted small" }, `Values: ${o.values.join(", ")} · ${o.horizontal ? "horizontal" : "vertical"}`),
      custom
        ? this.dropdown("Direction (how values are laid out)", raw.direction || "vertical", [["horizontal", "horizontal: values left→right, fields stacked"], ["vertical", "vertical: values top→bottom, fields side by side"]], (v) => set(() => (raw.direction = v)))
        : null,
      this.input("Field labels (e.g. q1..20 or roll1..6, comma separated)", (raw.fieldLabels || []).join(", "), (v) =>
        set(() => (raw.fieldLabels = v.split(",").map((s) => s.trim()).filter(Boolean)))
      ),
      el("div", { class: "muted small", style: { marginBottom: "8px" } }, `${o.nF} field(s): ${o.labels.slice(0, 4).join(", ")}${o.nF > 4 ? " … " + o.labels[o.nF - 1] : ""}`),
      el(
        "div",
        { class: "two" },
        this.num("Origin x", raw.origin?.[0], (v) => set(() => (raw.origin = [Math.round(v), raw.origin[1]]))),
        this.num("Origin y", raw.origin?.[1], (v) => set(() => (raw.origin = [raw.origin[0], Math.round(v)]))),
        this.num("Bubbles gap", raw.bubblesGap, (v) => set(() => (raw.bubblesGap = v)), { step: "0.1" }),
        this.num("Labels gap", raw.labelsGap, (v) => set(() => (raw.labelsGap = v)), { step: "0.1" }),
        this.num("Bubble width", raw.bubbleDimensions?.[0], (v) => set(() => (v === null ? delete raw.bubbleDimensions : (raw.bubbleDimensions = [v, raw.bubbleDimensions?.[1] ?? o.bh]))), { optional: true, placeholder: `page: ${o.bw}` }),
        this.num("Bubble height", raw.bubbleDimensions?.[1], (v) => set(() => (v === null ? delete raw.bubbleDimensions : (raw.bubbleDimensions = [raw.bubbleDimensions?.[0] ?? o.bw, v]))), { optional: true, placeholder: `page: ${o.bh}` })
      ),
      this.input("Empty value", raw.emptyValue ?? "", (v) => set(() => (v === "" ? delete raw.emptyValue : (raw.emptyValue = v))), { placeholder: "inherit" }),
      el("div", { class: "muted small" }, `Size ${Math.round(o.w)} × ${Math.round(o.h)} px`),
      this.dropdown(
        "Fit to printed border (rectifyOnBorder)",
        raw.rectifyOnBorder === undefined ? "" : String(raw.rectifyOnBorder),
        [["", "config default (alignment_params.rectify_on_border)"], ["true", "on: snap bubbles onto the box printed around the block"], ["false", "off"]],
        (v) => set(() => (v === "" ? delete raw.rectifyOnBorder : (raw.rectifyOnBorder = v === "true")))
      ),
      raw.rectifyOnBorder
        ? this.num("Border gap (px from bubbles to the box)", typeof raw.borderPadding === "number" ? raw.borderPadding : raw.borderPadding?.[0], (v) => set(() => (v === null ? delete raw.borderPadding : (raw.borderPadding = v))), { optional: true, placeholder: "estimate per sheet" })
        : null,
      blockAlignmentFields(this, raw)
    );
    return box;
  }

  renderZone(o) {
    const raw = o.raw;
    raw.options = raw.options || {};
    const opts = raw.options;
    const set = (fn) => this.edit(fn);
    const setOpt = (k, v) => set(() => (v === "" || v === null || v === undefined ? delete opts[k] : (opts[k] = v)));
    const box = el("div", {});
    box.append(
      el("h3", {}, "Zone"),
      this.input("Name", o.name, (v) => {
        v = v.trim();
        if (!v || v === o.name) return;
        if (this.doc.zones[v] || this.allLabels().has(v)) return toast("That name is already used", "error");
        set(() => {
          this.renameKey("zones", o.name, v);
          this.selected = { kind: "zone", name: v };
        });
      }),
      this.dropdown("Type", raw.type, (state.caps?.zone_types || ["barcode", "qrcode", "ocr", "icr"]).map((t) => [t, t]), (v) => set(() => (raw.type = v))),
      el(
        "div",
        { class: "two" },
        this.num("x", o.x, (v) => set(() => (raw.origin = [Math.round(v), raw.origin[1]]))),
        this.num("y", o.y, (v) => set(() => (raw.origin = [raw.origin[0], Math.round(v)]))),
        this.num("Width", o.w, (v) => set(() => (raw.dimensions = [Math.round(v), raw.dimensions[1]]))),
        this.num("Height", o.h, (v) => set(() => (raw.dimensions = [raw.dimensions[0], Math.round(v)])))
      )
    );
    if (raw.type === "barcode" || raw.type === "qrcode") {
      const formats = state.caps?.barcode_formats || [];
      const chosen = new Set(opts.formats || []);
      const list = el(
        "div",
        { class: "formats" },
        formats.map((f) => {
          const cb = el("input", { type: "checkbox", checked: chosen.has(f) });
          cb.addEventListener("change", () => {
            const next = new Set(opts.formats || []);
            cb.checked ? next.add(f) : next.delete(f);
            setOpt("formats", next.size ? [...next] : null);
          });
          return el("label", {}, cb, " ", f);
        })
      );
      box.append(el("div", { class: "field" }, "Accepted formats (none ticked = all)", list));
    }
    if (raw.type === "ocr" || raw.type === "icr") {
      box.append(
        this.input("Allowed characters", opts.whitelist ?? "", (v) => setOpt("whitelist", v), { placeholder: "e.g. 0123456789" }),
        raw.type === "icr" ? this.num("Character boxes", opts.characterBoxes, (v) => setOpt("characterBoxes", v === null ? null : Math.max(1, Math.round(v))), { optional: true }) : null,
        raw.type === "ocr"
          ? this.num("Page seg. mode", opts.psm, (v) => setOpt("psm", v === null ? null : Math.min(13, Math.round(v))), { optional: true, placeholder: "7" })
          : null
      );
    }
    // Direction, engines, language, image zone options (editor_ocr.js)
    box.append(...ocrZoneControls(this, raw, opts, setOpt, o.name));
    box.append(
      this.input("Pattern (regex the value must match)", opts.pattern ?? "", (v) => {
        try {
          if (v) new RegExp(v);
          setOpt("pattern", v);
        } catch (e) {
          toast("Invalid regular expression", "error");
        }
      }),
      el(
        "div",
        { class: "two" },
        this.num("Min confidence", opts.minConfidence, (v) => setOpt("minConfidence", v === null ? null : Math.min(1, v)), { optional: true, step: "0.05", placeholder: "0.6" }),
        this.input("Empty value", opts.emptyValue ?? "", (v) => setOpt("emptyValue", v))
      ),
      this.dropdown(
        "Colour removal for this zone",
        typeof opts.colorDropout === "object" && opts.colorDropout ? "page-json" : opts.colorDropout || "",
        [["", "same as the page"], ["grey", "none (plain grey)"], ["red", "red channel"], ["green", "green channel"], ["blue", "blue channel"], ["max", "brightest channel"], ...(typeof opts.colorDropout === "object" && opts.colorDropout ? [["page-json", JSON.stringify(opts.colorDropout)]] : [])],
        (v) => v !== "page-json" && setOpt("colorDropout", v === "" ? null : v === "grey" ? "grey" : { mode: v })
      )
    );
    return box;
  }

  jsonArea(label, value, onchange, rows = 4) {
    const area = el("textarea", { rows, spellcheck: "false" });
    area.value = value;
    area.addEventListener("change", () => onchange(area.value, area));
    return el("label", { class: "field" }, label, area);
  }

  renderPage() {
    const doc = this.doc;
    const set = (fn) => this.edit(fn);
    const parse = (text, fallback) => {
      if (!text.trim()) return fallback;
      return JSON.parse(text);
    };
    const jsonSetter = (key, fallback) => (text) => {
      try {
        const value = parse(text, fallback);
        set(() => (value === undefined ? delete doc[key] : (doc[key] = value)));
      } catch (e) {
        toast(`${key}: ${e.message}`, "error");
      }
    };
    return el(
      "div",
      {},
      el("h3", {}, "Page"),
      el("p", { class: "muted small" }, "Click a block or zone to edit it. Use + Block / + Zone (or B / Z) and drag on the page to add one."),
      el(
        "div",
        { class: "two" },
        this.num("Page width", doc.pageDimensions?.[0], (v) => set(() => (doc.pageDimensions = [Math.round(v), doc.pageDimensions[1]]))),
        this.num("Page height", doc.pageDimensions?.[1], (v) => set(() => (doc.pageDimensions = [doc.pageDimensions[0], Math.round(v)]))),
        this.num("Bubble width", doc.bubbleDimensions?.[0], (v) => set(() => (doc.bubbleDimensions = [Math.round(v), doc.bubbleDimensions[1]]))),
        this.num("Bubble height", doc.bubbleDimensions?.[1], (v) => set(() => (doc.bubbleDimensions = [doc.bubbleDimensions[0], Math.round(v)])))
      ),
      this.input("Empty value", doc.emptyValue ?? "", (v) => set(() => (doc.emptyValue = v))),
      el(
        "div",
        { class: "field" },
        "Colour removal (colorDropout)",
        el(
          "div",
          { class: "row gap" },
          el("code", {}, doc.colorDropout ? JSON.stringify(doc.colorDropout) : "grey (off)"),
          el("button", { class: "small", onclick: () => new ColourPanel(this).open() }, "Colours…")
        )
      ),
      this.renderThreshold(),
      alignmentSection(this),
      el("div", { class: "muted small" }, `${Object.keys(doc.fieldBlocks).length} blocks · ${this.allLabels().size} fields · ${Object.keys(doc.zones).length} zones`),
      el("h3", {}, "Advanced"),
      this.jsonArea("preProcessors (alignment / cleanup)", JSON.stringify(doc.preProcessors || [], null, 1), jsonSetter("preProcessors", [])),
      this.jsonArea("customLabels (joined columns)", JSON.stringify(doc.customLabels || {}, null, 1), jsonSetter("customLabels", undefined), 3),
      this.jsonArea("outputColumns (CSV order)", JSON.stringify(doc.outputColumns || [], null, 1), jsonSetter("outputColumns", undefined), 2),
      this.jsonArea("config.json (tuning, models; empty = defaults)", this.configText, (text) => {
        this.configText = text;
        this.dirty = true;
        this.updateButtons();
      }, 4),
      this.jsonArea("evaluation.json (answer key / scoring; empty = none)", this.evaluationText, (text) => {
        this.evaluationText = text;
        this.dirty = true;
        this.updateButtons();
      }, 4)
    );
  }

  // config.json values edited through the form (the JSON box stays the source)
  configObject() {
    if (!this.configText.trim()) return {};
    try {
      return JSON.parse(this.configText);
    } catch (e) {
      return null;
    }
  }

  setConfig(section, values) {
    const config = this.configObject();
    if (config === null) return toast("config.json below is not valid JSON; fix it first", "error");
    const next = { ...(config[section] || {}) };
    for (const [k, v] of Object.entries(values)) {
      if (v === null || v === undefined) delete next[k];
      else next[k] = v;
    }
    if (Object.keys(next).length) config[section] = next;
    else delete config[section];
    this.configText = Object.keys(config).length ? JSON.stringify(config, null, 2) : "";
    this.dirty = true;
    this.updateButtons();
    this.renderSide();
  }

  renderThreshold() {
    const config = this.configObject() || {};
    const t = config.threshold_params || {};
    const fixed = t.mode === "fixed";
    return el(
      "div",
      {},
      this.dropdown(
        "Bubble threshold",
        fixed ? "fixed" : "adaptive",
        [["adaptive", "adaptive (per sheet and per row)"], ["fixed", "fixed intensity line"]],
        (v) => this.setConfig("threshold_params", v === "fixed" ? { mode: "fixed", fixed_threshold: t.fixed_threshold ?? 120, fixed_min_fill_ratio: t.fixed_min_fill_ratio ?? 0.12 } : { mode: null, fixed_threshold: null, fixed_min_fill_ratio: null })
      ),
      fixed
        ? el(
            "div",
            { class: "two" },
            this.num("Dark below (0–255)", t.fixed_threshold ?? 120, (v) => this.setConfig("threshold_params", { fixed_threshold: Math.min(255, v) })),
            this.num("Min filled share (0–1)", t.fixed_min_fill_ratio ?? 0.12, (v) => this.setConfig("threshold_params", { fixed_min_fill_ratio: Math.min(1, v) }), { step: "0.01" })
          )
        : null
    );
  }

  // ------------------------------------------------------------------ report
  parseReport(report) {
    if (!report || typeof report !== "object") return null;
    const items = [];
    const warnings = [];
    const describe = (x) => {
      if (typeof x === "string") return { text: x, names: [] };
      const name = x.name || x.block || x.zone || x.field || x.label || x.target || x.id;
      const names = [name, ...(Array.isArray(x.fields) ? x.fields : [])].filter((n) => typeof n === "string");
      const text = x.reason || x.message || x.issue || x.note || x.description || x.detail || "";
      return { text: text || JSON.stringify(x), names, title: name };
    };
    for (const k of REPORT_KEYS) {
      if (Array.isArray(report[k])) items.push(...report[k].map(describe));
    }
    if (Array.isArray(report.warnings)) warnings.push(...report.warnings.map((w) => describe(w).text));
    const summary = Object.entries(report).filter(([k, v]) => !REPORT_KEYS.includes(k) && k !== "warnings" && (typeof v !== "object" || v === null));
    return { items, warnings, summary };
  }

  resolveName(name) {
    if (this.doc.fieldBlocks[name]) return { kind: "block", name };
    if (this.doc.zones[name]) return { kind: "zone", name };
    for (const block of Object.keys(this.doc.fieldBlocks)) {
      if (this.blockInfo(block).labels.includes(name)) return { kind: "block", name: block };
    }
    return null;
  }

  highlightNames() {
    const names = new Set();
    if (!this.report) return names;
    this.report.items.forEach((item, i) => {
      if (this.resolved.has(i)) return;
      for (const n of item.names) {
        const target = this.resolveName(n);
        if (target) names.add(target.name);
      }
    });
    return names;
  }

  renderReport() {
    const r = this.report;
    const box = el("div", { class: "ed-report" }, el("h3", {}, `Needs verification (${r.items.length - [...this.resolved].length})`));
    if (r.summary.length) box.append(el("div", { class: "muted small" }, r.summary.map(([k, v]) => `${k}: ${v}`).join(" · ")));
    box.append(
      el(
        "ul",
        {},
        r.items.map((item, i) =>
          el(
            "li",
            {
              class: this.resolved.has(i) ? "resolved" : "",
              title: "Click to select · double-click to mark verified",
              onclick: () => {
                const target = item.names.map((n) => this.resolveName(n)).find(Boolean);
                if (target) {
                  this.selected = target;
                  this.renderSide();
                  this.centerOn(this.info(target));
                }
              },
              ondblclick: () => {
                this.resolved.has(i) ? this.resolved.delete(i) : this.resolved.add(i);
                this.renderSide();
                this.draw();
              },
            },
            item.title ? el("strong", {}, item.title + ": ") : null,
            item.text
          )
        )
      )
    );
    box.append(renderWarnings(this, r.warnings)); // confirm-to-clear (generator_warnings.js)
    return box;
  }

  centerOn(o) {
    if (!o) return;
    const r = this.wrap.getBoundingClientRect();
    this.view.ox = r.width / 2 - (o.x + o.w / 2) * this.view.scale;
    this.view.oy = r.height / 2 - (o.y + o.h / 2) * this.view.scale;
    this.draw();
  }

  renderTest() {
    const t = this.testResult;
    const flagged = t.review || [];
    return el(
      "div",
      {},
      el("h3", {}, "Test read"),
      el("div", { class: "row gap wrap" }, el("span", { class: `chip ${t.status}` }, t.status), el("span", { class: "muted small" }, t.file_id), t.score !== null && t.score !== undefined ? el("span", { class: "chip" }, `score ${t.score}`) : null),
      t.error ? el("div", { class: "chip error" }, t.error) : null,
      el(
        "ul",
        { class: "error-list", style: { color: "var(--warn)" } },
        flagged.slice(0, 30).map((item) =>
          el(
            "li",
            {
              style: { cursor: "pointer" },
              onclick: () => {
                const target = this.resolveName(item.name);
                if (target) {
                  this.selected = target;
                  this.renderSide();
                  this.centerOn(this.info(target));
                }
              },
            },
            `${item.name}: ${(item.flags || []).join(", ")}`
          )
        )
      ),
      el("button", { class: "small ghost", onclick: () => { this.testResult = null; if (this.bgMode === "scan") { this.bgMode = this.detail.has_reference ? "reference" : "blank"; this.loadBackground(); } this.renderSide(); this.draw(); } }, "Clear test overlay")
    );
  }

  // ------------------------------------------------------------------ server
  async save() {
    const body = { template: this.doc };
    try {
      if (this.configText !== this.savedConfigText) body.config = this.configText.trim() ? JSON.parse(this.configText) : null;
      if (this.evaluationText !== this.savedEvaluationText) body.evaluation = this.evaluationText.trim() ? JSON.parse(this.evaluationText) : null;
    } catch (e) {
      toast(`config/evaluation JSON: ${e.message}`, "error");
      return false;
    }
    try {
      const detail = await api(`/templates/${this.id}`, { method: "PUT", json: body });
      this.detail = { ...this.detail, ...detail };
      this.statusChip.className = `chip ${detail.status}`;
      this.statusChip.textContent = detail.status;
      this.errors = [];
      this.dirty = false;
      this.savedConfigText = this.configText;
      this.savedEvaluationText = this.evaluationText;
      this.updateButtons();
      this.renderSide();
      toast("Template saved", "ok");
      return true;
    } catch (error) {
      this.errors = error.errors && error.errors.length ? error.errors : [{ path: "$", message: error.message }];
      this.renderSide();
      toast("Template not saved: see the errors in the panel", "error");
      return false;
    }
  }

  async testRead(file) {
    this.testInput.value = "";
    if (!file) return;
    if (this.dirty && !(await this.save())) return;
    const form = new FormData();
    form.append("template_id", this.id);
    form.append("files", file, file.name);
    try {
      toast("Reading sample…");
      const data = await api("/scans", { method: "POST", form });
      this.testResult = data.scans[0];
      this.testOverlay = true;
      if (this.testResult.links?.aligned) {
        this.bgMode = "scan";
        await this.loadBackground();
      }
      this.renderSide();
      this.draw();
      const flagged = (this.testResult.review || []).length;
      toast(`Read ${Object.keys(this.testResult.fields || {}).length} fields, ${flagged} flagged`, flagged ? "" : "ok");
    } catch (error) {
      toast(error.message, "error", 6000);
    }
  }
}
