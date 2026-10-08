// Alignment panel of the template editor (plan item 10): shows the timing
// tracks, corner markers and index points the generator found, numbered on the
// page, and lets the user define them: pick the method, drag a box over a strip
// to add a track, click marks to remove / add them, click or box index points
// (small printed dots too), and "Test on samples" re-runs alignment on the
// sheets kept from generation.
//
// Hooks in editor.js: drawAlignment(ed) in draw(), alignmentPick(ed, rect) in
// onUp() for the "align-*" modes, renderAlignmentPanel(ed) under the Page panel.
import { api, el, toast } from "./api.js";

const TRACK_COLOR = "#e0731f";
const MISSED_COLOR = "#d0342c";
const INDEX_COLOR = "#c2188b";
const CANDIDATE_COLOR = "rgba(194,24,139,0.55)";
const BOX_COLOR = "rgba(224,115,31,0.8)";
const MODES = {
  "align-track": "Drag a box over a strip of timing marks",
  "align-marks": "Click a mark to remove it, or empty space on a track to add one",
  "align-index": "Click a printed mark (or drag a box around it) to add an index point",
};
const METHODS = [
  ["tracks", "Timing tracks", "Rows of solid marks along the edges; the most robust choice when the form has them."],
  ["markers", "Corner markers", "Four same-size solid squares near the corners, used like a short track."],
  ["page", "Page edges", "The paper's outline; for scans with background around the page."],
  ["image", "Reference image", "Match the whole printed form to the reference image (ECC)."],
  ["none", "None", "The scans are already straight and cropped to the page."],
];
const ALIGNERS = ["TimingMarkAlignment", "CropOnMarkers", "CropPage", "EccAlignment", "FeatureBasedAlignment"];

let stylesAdded = false;
function addStyles() {
  if (stylesAdded) return;
  stylesAdded = true;
  document.head.append(
    el(
      "style",
      {},
      `.al-panel{border-top:1px solid var(--line,#ddd);margin-top:10px;padding-top:6px}
       .al-help{display:inline-block;margin-left:4px;color:var(--muted,#777);cursor:help;font-size:12px}
       .al-hint{display:none;font-size:12px;color:var(--muted,#777);margin:2px 0 6px}
       .al-field:focus-within .al-hint,.al-field:hover .al-hint{display:block}
       .al-list{list-style:none;padding:0;margin:4px 0;font-size:12.5px}
       .al-list li{display:flex;gap:6px;align-items:center;padding:2px 0;flex-wrap:wrap}
       .al-bad{color:${MISSED_COLOR};font-weight:600}
       .al-ok{color:#1f7a3f}
       .al-sheets{font-size:12px;border-collapse:collapse;width:100%}
       .al-sheets td,.al-sheets th{padding:1px 4px;text-align:left;border-bottom:1px solid var(--line,#eee)}
       .al-mode{background:#fff4e5;border:1px solid #f0c48a;padding:4px 6px;font-size:12px;border-radius:4px;margin:4px 0}`
    )
  );
}

// A labelled control with an info icon and a help line shown when focused.
function field(label, control, help) {
  return el(
    "label",
    { class: "field al-field" },
    el("span", {}, label, el("span", { class: "al-help", title: help }, "ⓘ")),
    control,
    el("div", { class: "al-hint" }, help)
  );
}

// ---------------------------------------------------------------- model
function processors(ed) {
  if (!Array.isArray(ed.doc.preProcessors)) ed.doc.preProcessors = [];
  return ed.doc.preProcessors;
}

export function timing(ed) {
  return (ed.doc.preProcessors || []).find((p) => p.name === "TimingMarkAlignment") || null;
}

function generatorReport(ed) {
  const r = ed.detail && ed.detail.report;
  return r && typeof r === "object" ? r : {};
}

function alignmentReport(ed) {
  return generatorReport(ed).alignment || {};
}

function currentMethod(ed) {
  const names = (ed.doc.preProcessors || []).map((p) => p.name);
  const t = timing(ed);
  if (t) return Object.keys(t.options?.tracks || {}).join() === "corners" ? "markers" : "tracks";
  if (names.includes("CropOnMarkers")) return "markers";
  if (names.includes("CropPage")) return "page";
  if (names.includes("EccAlignment") || names.includes("FeatureBasedAlignment")) return "image";
  return "none";
}

function pitchOf(marks) {
  if (!marks || marks.length < 2) return 0;
  const gaps = [];
  for (let i = 1; i < marks.length; i++) gaps.push(Math.hypot(marks[i][0] - marks[i - 1][0], marks[i][1] - marks[i - 1][1]));
  gaps.sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)];
}

function isVertical(marks) {
  if (!marks || marks.length < 2) return true;
  const xs = marks.map((m) => m[0]);
  const ys = marks.map((m) => m[1]);
  return Math.max(...ys) - Math.min(...ys) >= Math.max(...xs) - Math.min(...xs);
}

function trackName(ed, marks, tracks) {
  const [pw, ph] = ed.page();
  const xs = marks.map((m) => m[0]);
  const ys = marks.map((m) => m[1]);
  const cx = xs.reduce((a, b) => a + b, 0) / xs.length;
  const cy = ys.reduce((a, b) => a + b, 0) / ys.length;
  let base = isVertical(marks) ? (cx < pw / 2 ? "left" : "right") : cy < ph / 2 ? "top" : "bottom";
  let name = base;
  for (let i = 2; tracks[name]; i++) name = `${base}_${i}`;
  return name;
}

function setMethod(ed, method) {
  const report = alignmentReport(ed);
  ed.edit(() => {
    const kept = processors(ed).filter((p) => !ALIGNERS.includes(p.name));
    const old = timing(ed);
    let aligner = null;
    if (method === "tracks") {
      const tracks = {};
      const fromReport = report.tracks || {};
      const oldTracks = old && currentMethod(ed) === "tracks" ? old.options.tracks : null;
      for (const [name, t] of Object.entries(oldTracks || fromReport)) tracks[name] = { marks: t.marks };
      const dims = old?.options?.markDimensions || Object.values(fromReport)[0]?.mark_dimensions || [20, 10];
      aligner = { name: "TimingMarkAlignment", options: { ...(old?.options || {}), tracks, markDimensions: dims } };
      if (!Object.keys(tracks).length) toast("No tracks yet: click “Add track” and drag a box over a strip of marks");
    } else if (method === "markers") {
      const corners = Object.values(report.corner_markers || {});
      if (corners.length < 3) {
        toast("Fewer than 3 corner markers were found: add them as a track with “Add track”, or pick another method", "error", 6000);
      }
      const dims = corners[0]?.dimensions || [30, 30];
      aligner = {
        name: "TimingMarkAlignment",
        options: { tracks: corners.length >= 2 ? { corners: { marks: corners.map((c) => c.centre) } } : {}, markDimensions: dims.map(Number) },
      };
    } else if (method === "page") {
      aligner = { name: "CropPage", options: { morphKernel: [10, 10] } };
    } else if (method === "image") {
      aligner = { name: "EccAlignment", options: { reference: "reference.png", motion: "affine" } };
    }
    ed.doc.preProcessors = aligner ? [aligner, ...kept] : kept;
  });
}

function editTracks(ed, fn) {
  ed.edit(() => {
    let t = timing(ed);
    if (!t) {
      t = { name: "TimingMarkAlignment", options: { tracks: {}, markDimensions: [20, 10] } };
      processors(ed).unshift(t);
    }
    t.options.tracks = t.options.tracks || {};
    fn(t.options);
  });
}

// Typed positions for index points (exact X/Y and size, in template pixels)
function numberInputs(ed, i, p) {
  const box = el("span", { class: "al-xy" });
  const parts = [
    ["X", () => p.center[0], (o, v) => (o.indexPoints[i].center = [v, o.indexPoints[i].center[1]])],
    ["Y", () => p.center[1], (o, v) => (o.indexPoints[i].center = [o.indexPoints[i].center[0], v])],
    ["W", () => p.size[0], (o, v) => (o.indexPoints[i].size = [Math.max(1, v), o.indexPoints[i].size[1]])],
    ["H", () => p.size[1], (o, v) => (o.indexPoints[i].size = [o.indexPoints[i].size[0], Math.max(1, v)])],
  ];
  for (const [label, get, set] of parts) {
    const input = el("input", { type: "number", class: "small", step: "1", value: Math.round(get()), title: `${label} in template pixels`, style: "width:4.5em" });
    input.addEventListener("change", () => {
      const v = Number(input.value);
      if (!Number.isFinite(v)) return;
      editTracks(ed, (o) => set(o, v));
      ed.draw();
    });
    box.append(label, input);
  }
  return box;
}

function typedIndexPoint(ed) {
  const inputs = ["X", "Y", "W", "H"].map((label) => el("input", { type: "number", class: "small", step: "1", placeholder: label, title: `${label} in template pixels`, style: "width:4.5em" }));
  const addTyped = async () => {
    const [x, y, w, h] = inputs.map((i) => Number(i.value));
    if (!inputs[0].value || !inputs[1].value) return toast("Type X and Y first");
    let size = [w || 0, h || 0];
    let shape = "any";
    try {
      // Look for a printed mark at that spot on the reference sheet
      const mark = await api(`/templates/${ed.id}/generator/find-mark`, { method: "POST", json: { point: [x, y] } });
      const off = Math.hypot(mark.center[0] - x, mark.center[1] - y);
      if (!size[0] || !size[1]) size = mark.size;
      shape = mark.shape || "any";
      toast(`Mark found ${Math.round(off)} px from the typed point (${mark.size[0]}×${mark.size[1]})`, off > Math.max(...mark.size) ? "error" : "ok", 5000);
    } catch (error) {
      if (!size[0] || !size[1]) return toast(`No mark found there; type its W and H too. ${error.message}`, "error", 5000);
      toast("No mark found at that spot on the reference sheet; added as typed", "error", 5000);
    }
    editTracks(ed, (o) => {
      const points = (o.indexPoints = o.indexPoints || []);
      let n = points.length + 1;
      while (points.some((p) => p.name === `P${n}`)) n++;
      points.push({ name: `P${n}`, center: [x, y], size, shape, required: true });
    });
    ed.draw();
  };
  return el("div", { class: "row gap small", title: "Add an index point at an exact position" }, "Add at", ...inputs, el("button", { class: "small", onclick: addTyped }, "Add"));
}

// ---------------------------------------------------------------- picking
export async function alignmentPick(ed, rect) {
  const mode = ed.mode;
  const click = rect.w <= 4 && rect.h <= 4;
  const point = [rect.x + rect.w / 2, rect.y + rect.h / 2];
  try {
    if (mode === "align-track") {
      if (click) return toast("Drag a box over the whole strip of marks");
      const track = await api(`/templates/${ed.id}/generator/find-track`, { method: "POST", json: { box: [rect.x, rect.y, rect.w, rect.h] } });
      editTracks(ed, (o) => {
        const name = trackName(ed, track.marks, o.tracks);
        o.tracks[name] = { marks: track.marks };
        if (!o.markDimensions || Object.keys(o.tracks).length === 1) o.markDimensions = track.mark_dimensions;
        toast(`${name}: ${track.marks.length} marks, pitch ${Math.round(track.pitch)} px`, "ok");
      });
      ed.setMode(null);
    } else if (mode === "align-marks") {
      await toggleMark(ed, point);
    } else if (mode === "align-index") {
      const body = click ? { point } : { box: [rect.x, rect.y, rect.w, rect.h] };
      const mark = await api(`/templates/${ed.id}/generator/find-mark`, { method: "POST", json: body });
      editTracks(ed, (o) => {
        const points = (o.indexPoints = o.indexPoints || []);
        let n = points.length + 1;
        while (points.some((p) => p.name === `P${n}`)) n++;
        points.push({ name: `P${n}`, center: mark.center, size: mark.size, shape: mark.shape || "any", required: true });
        toast(`Index point P${n}: ${mark.size[0]}×${mark.size[1]} px ${mark.shape}`, "ok");
      });
    }
  } catch (error) {
    toast(error.message, "error", 5000);
  }
  ed.draw();
}

async function toggleMark(ed, point) {
  const t = timing(ed);
  if (!t) return toast("Add a track first");
  const tracks = t.options.tracks || {};
  // Remove the nearest mark when the click is on one
  let best = null;
  for (const [name, track] of Object.entries(tracks)) {
    const pitch = pitchOf(track.marks) || 30;
    track.marks.forEach((m, i) => {
      const d = Math.hypot(m[0] - point[0], m[1] - point[1]);
      if (d < 0.45 * pitch && (!best || d < best.d)) best = { name, i, d };
    });
  }
  if (best) {
    editTracks(ed, (o) => {
      const marks = o.tracks[best.name].marks;
      if (marks.length <= 2) return toast("A track needs at least 2 marks; remove the whole track instead", "error");
      marks.splice(best.i, 1);
    });
    return;
  }
  // Otherwise add one on the nearest track, centred on the printed mark
  let near = null;
  for (const [name, track] of Object.entries(tracks)) {
    const vertical = isVertical(track.marks);
    const across = vertical ? track.marks.map((m) => m[0]) : track.marks.map((m) => m[1]);
    const line = across.reduce((a, b) => a + b, 0) / across.length;
    const d = Math.abs((vertical ? point[0] : point[1]) - line);
    if (!near || d < near.d) near = { name, d, vertical };
  }
  const dims = t.options.markDimensions || [20, 10];
  if (!near || near.d > 2 * Math.max(...dims)) return toast("Click on a track (or on one of its marks to remove it)");
  let centre = point;
  try {
    const mark = await api(`/templates/${ed.id}/generator/find-mark`, { method: "POST", json: { point } });
    centre = mark.center;
  } catch (error) {
    toast("No printed mark there; added at the click", "", 3000);
  }
  editTracks(ed, (o) => {
    const marks = o.tracks[near.name].marks;
    marks.push([Math.round(centre[0] * 10) / 10, Math.round(centre[1] * 10) / 10]);
    marks.sort((a, b) => (near.vertical ? a[1] - b[1] : a[0] - b[0]));
  });
}

// ---------------------------------------------------------------- drawing
function missedMarks(ed) {
  // Marks missed on some sample: last "Test on samples" run, else the generator's
  const sheets = (ed.alignmentTest && ed.alignmentTest.sheets) || alignmentReport(ed).sheets || [];
  const count = new Map();
  for (const s of sheets) for (const m of s.missed || []) {
    const key = `${Math.round(m[0])},${Math.round(m[1])}`;
    count.set(key, (count.get(key) || 0) + 1);
  }
  return count;
}

export function drawAlignment(ed) {
  if (ed.showAlignment === false || !ed.ctx) return;
  const ctx = ed.ctx;
  const { scale, ox, oy } = ed.view;
  const X = (v) => ox + v * scale;
  const Y = (v) => oy + v * scale;
  const t = timing(ed);
  const report = alignmentReport(ed);
  const missed = missedMarks(ed);
  ctx.save();
  ctx.font = "10px system-ui, sans-serif";
  if (t) {
    const [mw, mh] = t.options.markDimensions || [20, 10];
    for (const [name, track] of Object.entries(t.options.tracks || {})) {
      const vertical = isVertical(track.marks);
      const [w, h] = vertical ? [mw, mh] : [mh, mw];
      const pitch = pitchOf(track.marks) * scale;
      const every = pitch > 16 ? 1 : pitch > 6 ? 5 : 10;
      track.marks.forEach((m, i) => {
        const key = `${Math.round(m[0])},${Math.round(m[1])}`;
        const bad = missed.has(key);
        ctx.strokeStyle = bad ? MISSED_COLOR : TRACK_COLOR;
        ctx.lineWidth = bad ? 2 : 1.25;
        ctx.strokeRect(X(m[0] - w / 2), Y(m[1] - h / 2), w * scale, h * scale);
        if ((i + 1) % every === 0 || i === 0 || bad) {
          ctx.fillStyle = bad ? MISSED_COLOR : TRACK_COLOR;
          const tx = vertical ? (m[0] < ed.page()[0] / 2 ? X(m[0] + w / 2) + 3 : X(m[0] - w / 2) - 16) : X(m[0]) - 4;
          const ty = vertical ? Y(m[1]) + 3 : m[1] < ed.page()[1] / 2 ? Y(m[1] + h / 2) + 10 : Y(m[1] - h / 2) - 3;
          ctx.fillText(bad ? `${i + 1}×${missed.get(key)}` : String(i + 1), tx, ty);
        }
      });
      const first = track.marks[0];
      if (first) {
        ctx.fillStyle = TRACK_COLOR;
        ctx.font = "bold 11px system-ui, sans-serif";
        ctx.fillText(`${name} (${track.marks.length})`, X(first[0]) - 10, Y(first[1]) - 14);
        ctx.font = "10px system-ui, sans-serif";
      }
    }
    for (const p of t.options.indexPoints || []) {
      const [w, h] = p.size || [16, 16];
      ctx.strokeStyle = INDEX_COLOR;
      ctx.lineWidth = 2;
      ctx.beginPath();
      if (p.shape === "circle") ctx.ellipse(X(p.center[0]), Y(p.center[1]), (w / 2 + 3) * scale, (h / 2 + 3) * scale, 0, 0, Math.PI * 2);
      else ctx.rect(X(p.center[0] - w / 2 - 3), Y(p.center[1] - h / 2 - 3), (w + 6) * scale, (h + 6) * scale);
      ctx.stroke();
      ctx.fillStyle = INDEX_COLOR;
      ctx.font = "bold 11px system-ui, sans-serif";
      ctx.fillText(p.name || "P", X(p.center[0] + w / 2) + 5, Y(p.center[1]) - 5);
      ctx.font = "10px system-ui, sans-serif";
    }
  }
  if (ed.mode === "align-index") {
    // Suggestions: every isolated solid mark the generator saw
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = CANDIDATE_COLOR;
    for (const c of report.index_candidates || []) {
      const r = Math.max(...c.size) / 2 + 5;
      ctx.beginPath();
      ctx.arc(X(c.center[0]), Y(c.center[1]), r * scale, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }
  if (ed.showPrintedBoxes) {
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = BOX_COLOR;
    ctx.lineWidth = 1.5;
    for (const b of generatorReport(ed).printed_boxes || []) {
      if (!b.adoptable) continue;
      ctx.strokeRect(X(b.box[0]), Y(b.box[1]), b.box[2] * scale, b.box[3] * scale);
    }
    ctx.setLineDash([]);
  }
  ctx.restore();
}

// ---------------------------------------------------------------- panel
async function testOnSamples(ed, button) {
  if (ed.dirty && !(await ed.save())) return;
  button.disabled = true;
  button.textContent = "Testing…";
  try {
    ed.alignmentTest = await api(`/templates/${ed.id}/generator/test-alignment`, { method: "POST" });
    const bad = ed.alignmentTest.sheets.filter((s) => s.status === "error" || (s.expected && s.found < s.expected)).length;
    toast(`Alignment tested on ${ed.alignmentTest.tested} sheet(s)${bad ? `, ${bad} with missed marks or errors` : ""}`, bad ? "" : "ok");
  } catch (error) {
    toast(error.message, "error", 6000);
  }
  button.disabled = false;
  ed.renderSide();
  ed.draw();
}

async function adoptBox(ed, entry) {
  try {
    const data = await api(`/templates/${ed.id}/generator/adopt-box`, { method: "POST", json: { box: entry.box } });
    ed.edit(() => {
      const block = data.block;
      const start = ed.nextNumber("q");
      const n = data.fields;
      block.fieldLabels = [n > 1 ? `q${start}..${start + n - 1}` : `q${start}`];
      const name = ed.uniqueName(`Boxed_${Object.keys(ed.doc.fieldBlocks).length + 1}`, ed.doc.fieldBlocks);
      ed.doc.fieldBlocks[name] = block;
      entry.adoptable = false;
      entry.blocks = [name];
      ed.selected = { kind: "block", name };
    });
    toast(`Block added from the printed box (${data.rows}×${data.cols} bubbles): check its labels and values`, "ok", 6000);
  } catch (error) {
    toast(error.message, "error", 6000);
  }
}

function sheetName(ed, index) {
  const names = generatorReport(ed).sheet_names || [];
  return names[index] || `sheet ${index + 1}`;
}

function sheetsTable(ed, sheets, fromTest) {
  if (!sheets || !sheets.length) return null;
  return el(
    "table",
    { class: "al-sheets" },
    el("tr", {}, el("th", {}, "Sheet"), el("th", {}, "Marks found"), el("th", {}, "Index points"), fromTest ? el("th", {}, "Status") : null),
    sheets.map((s) => {
      const missed = (s.expected || 0) - (s.found || 0);
      const points = Object.entries(s.index_points || {});
      const lost = points.filter(([, ok]) => !ok).map(([n]) => n);
      return el(
        "tr",
        {},
        el("td", {}, fromTest ? s.file : sheetName(ed, s.sheet)),
        el("td", { class: missed ? "al-bad" : "al-ok", title: missed ? "Missed marks are drawn red on the page" : "" }, s.expected ? `${s.found} / ${s.expected}` : "–"),
        el("td", { class: lost.length ? "al-bad" : "al-ok" }, points.length ? (lost.length ? `missing ${lost.join(", ")}` : "all found") : "–"),
        fromTest
          ? el(
              "td",
              { class: s.status === "error" ? "al-bad" : "", title: s.error || "" },
              s.status + (s.registration?.orientation ? ` (turned ${s.registration.orientation}°)` : "")
            )
          : null
      );
    })
  );
}

export function renderAlignmentPanel(ed) {
  addStyles();
  const report = alignmentReport(ed);
  const method = currentMethod(ed);
  const t = timing(ed);
  const box = el("div", { class: "al-panel" });

  // The method itself is chosen in the Alignment section (editor_options.js);
  // this panel adds the track and index-point tools under it.

  const show = el("input", { type: "checkbox", checked: ed.showAlignment !== false, onchange: (e) => ((ed.showAlignment = e.target.checked), ed.draw()) });
  box.append(field("Show marks on the page", show, "Draws every timing mark (numbered) and index point the template uses; marks missed on a sample sheet are red."));

  if (ed.mode && MODES[ed.mode]) {
    box.append(el("div", { class: "al-mode" }, MODES[ed.mode], " · ", el("a", { href: "#", onclick: (e) => (e.preventDefault(), ed.setMode(null), ed.renderSide()) }, "done")));
  }
  const modeBtn = (mode, label, help) =>
    el("button", { class: `small${ed.mode === mode ? " active" : ""}`, title: help, onclick: () => (ed.setMode(mode), ed.renderSide(), ed.draw()) }, label);

  if (method === "tracks" || method === "markers") {
    const tracks = Object.entries(t?.options?.tracks || {});
    const summary = report.summary || [];
    box.append(
      el("div", { class: "muted small" }, summary.length ? summary.join(" · ") : ""),
      el(
        "ul",
        { class: "al-list" },
        tracks.map(([name, track]) =>
          el(
            "li",
            {},
            el("strong", {}, name),
            `${track.marks.length} marks, pitch ${Math.round(pitchOf(track.marks))} px`,
            el("button", { class: "small ghost", title: "Remove this track", onclick: () => editTracks(ed, (o) => delete o.tracks[name]) }, "Remove")
          )
        )
      ),
      el(
        "div",
        { class: "row gap wrap" },
        !tracks.length && Object.keys(report.tracks || {}).length
          ? el("button", { class: "small", title: "Fill in the tracks the generator found on the sample sheets", onclick: () => setMethod(ed, "tracks") }, "Use detected tracks")
          : null,
        modeBtn("align-track", "+ Add track", "Drag a box over a strip of timing marks; the marks inside are found on the reference image."),
        modeBtn("align-marks", "Edit marks", "Click a mark to remove it; click empty space on a track to add a mark there.")
      )
    );
    if (t) {
      const [mw, mh] = t.options.markDimensions || [20, 10];
      const num = (value, onchange, step = "1") => {
        const input = el("input", { type: "number", value, step, min: "0" });
        input.addEventListener("change", () => {
          const v = Number(input.value);
          if (Number.isFinite(v) && v > 0) onchange(v);
        });
        return input;
      };
      box.append(
        el(
          "div",
          { class: "two" },
          field("Mark width", num(mw, (v) => editTracks(ed, (o) => (o.markDimensions = [v, o.markDimensions?.[1] || mh]))), "Width of one mark on a left/right track, in page pixels (horizontal tracks use the same mark turned)."),
          field("Mark height", num(mh, (v) => editTracks(ed, (o) => (o.markDimensions = [o.markDimensions?.[0] || mw, v]))), "Height of one mark on a left/right track, in page pixels.")
        )
      );
      const orient = el("input", { type: "checkbox", checked: t.options.detectOrientation !== false, onchange: (e) => editTracks(ed, (o) => (e.target.checked ? delete o.detectOrientation : (o.detectOrientation = false))) });
      box.append(field("Detect upside-down sheets", orient, "Try the sheet turned 90/180/270°. With tracks that look the same upside down, add an index point so the right way up wins."));
    }
  }

  // Index points
  if (method !== "none") {
    const points = t?.options?.indexPoints || [];
    if (report.symmetric) {
      box.append(
        el(
          "div",
          { class: points.length ? "muted small" : "chip error" },
          points.length
            ? "The tracks look the same upside down: the index points below decide which way up a sheet is."
            : "The tracks look the same upside down: add an index point (any distinct printed mark, e.g. a small dot) so a sheet can't be read upside down."
        )
      );
    }
    box.append(
      el("h4", {}, "Index points", el("span", { class: "al-help", title: "Extra printed marks (squares, corner markers, small dots) matched together with the tracks. Each keeps its own size and shape." }, "ⓘ")),
      el(
        "ul",
        { class: "al-list" },
        points.map((p, i) => {
          const shape = el("select", { class: "small", title: "Shape of the printed mark" }, ["any", "square", "circle"].map((s) => el("option", { value: s, selected: s === (p.shape || "any") }, s)));
          shape.addEventListener("change", () => editTracks(ed, (o) => (o.indexPoints[i].shape = shape.value)));
          const required = el("input", { type: "checkbox", checked: p.required !== false, title: "Required: a sheet where this mark can't be found goes to review" });
          required.addEventListener("change", () => editTracks(ed, (o) => (o.indexPoints[i].required = required.checked)));
          return el(
            "li",
            {},
            el("strong", {}, p.name || `P${i + 1}`),
            numberInputs(ed, i, p),
            shape,
            el("label", { class: "small", title: "Required: a sheet where this mark can't be found goes to review" }, required, " required"),
            el("button", { class: "small ghost", onclick: () => editTracks(ed, (o) => o.indexPoints.splice(i, 1)) }, "Remove")
          );
        })
      ),
      modeBtn("align-index", "+ Add index point", "Click a printed mark on the page, or drag a box around it. Small dots work too. Suggestions are circled while this is on."),
      typedIndexPoint(ed)
    );
  }

  // Per-sheet results
  const generated = report.sheets;
  const tested = ed.alignmentTest?.sheets;
  box.append(
    el("h4", {}, tested ? "Test on samples" : "Marks matched per sample sheet"),
    sheetsTable(ed, tested || generated, Boolean(tested)) || el("div", { class: "muted small" }, "No sample results yet."),
    el(
      "button",
      { class: "small", title: "Save, then re-run this template's alignment on the sample sheets kept from generation", onclick: (e) => testOnSamples(ed, e.target) },
      "Test on samples"
    )
  );

  // Printed boxes that could become blocks (Gemini 37)
  const adoptable = (generatorReport(ed).printed_boxes || []).filter((b) => b.adoptable);
  if (adoptable.length) {
    const showBoxes = el("input", { type: "checkbox", checked: !!ed.showPrintedBoxes, onchange: (e) => ((ed.showPrintedBoxes = e.target.checked), ed.draw()) });
    box.append(
      el("h4", {}, "Printed boxes without a block"),
      field("Show them on the page", showBoxes, "Rectangles printed on the form that hold bubbles but no block yet (dashed orange)."),
      el(
        "ul",
        { class: "al-list" },
        adoptable.map((b) =>
          el(
            "li",
            {},
            `${b.box[2]}×${b.box[3]} at ${b.box[0]}, ${b.box[1]} · ${b.bubbles_inside} bubbles`,
            el("button", { class: "small", title: "Find the bubble grid inside this box and add it as a block", onclick: () => adoptBox(ed, b) }, "Adopt as block")
          )
        )
      ),
      adoptable.length > 1
        ? el("button", { class: "small ghost", onclick: async () => { for (const b of adoptable) await adoptBox(ed, b); } }, `Adopt all ${adoptable.length}`)
        : null
    );
  }
  return box;
}
