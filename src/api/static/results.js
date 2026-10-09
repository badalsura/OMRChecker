// Results tab: browse every graded sheet, see the exact overlay it was graded
// with (re-rendered by the server, drawn here), correct it by clicking bubbles
// or typing, verify it, regrade it with overrides, and read the accuracy.
import { api, chip, confidenceColor, el, emit, modal, on, safeStorage, state, toast, url } from "./api.js";
import { scoreSummary } from "./score_view.js";

const PAGE = 100;
const r = {
  filters: {},
  items: [],
  offset: 0,
  total: 0,
  pos: -1, // index of the open sheet in items
  data: null, // overlay payload of the open sheet
  image: null,
  preview: null, // {data, image} while a regrade preview is shown
  view: { scale: 1, x: 0, y: 0 },
  overlay: true,
  selected: null, // field/zone name
  scan: null, // {name, box} while a barcode/QR box is being drawn
  hover: null,
  busy: false,
  autoAdvance: safeStorage("get", "omr_res_advance") !== "0",
  prefetched: new Set(),
  // View choice, remembered between sheets (and sessions)
  mode: ["aligned", "color", "original"].includes(safeStorage("get", "omr_res_mode")) ? safeStorage("get", "omr_res_mode") : "aligned",
  side: safeStorage("get", "omr_res_side") === "1",
  left: ["aligned", "color", "original"].includes(safeStorage("get", "omr_res_left")) ? safeStorage("get", "omr_res_left") : "original",
  borders: safeStorage("get", "omr_res_borders") === "1",
  views: {}, // kind -> {scanId, meta, image, error, loading}
};

let canvas, ctx2d, statusLine;

const $ = (id) => document.getElementById(id);
const typing = (target) => target && target.matches && target.matches("input, select, textarea");

export function initResults() {
  canvas = $("res-canvas");
  ctx2d = canvas.getContext("2d");
  statusLine = $("res-status");
  for (const id of ["res-template", "res-view", "res-name", "res-flag", "res-job"]) {
    $(id).addEventListener("change", () => {
      if (id === "res-template" || id === "res-job") refreshFacets();
      load(0);
    });
  }
  let searchTimer;
  $("res-file").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => load(0), 300);
  });
  $("res-prev-page").addEventListener("click", () => load(Math.max(0, r.offset - PAGE)));
  $("res-next-page").addEventListener("click", () => load(r.offset + PAGE));
  $("res-accuracy").addEventListener("click", showAccuracy);
  $("res-remap").addEventListener("click", showRemap);
  $("res-export").addEventListener("click", () => emit("export-open", currentFilters()));
  bindCanvas();
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", () => {
    if (isActive()) {
      sizeCanvas();
      draw();
    }
  });
  on("templates", () => refreshFacets());
  on("results-job", (jobId) => {
    refreshJobs().then(() => {
      $("res-job").value = jobId;
      $("res-view").value = "all";
      document.querySelector('.tabs button[data-tab="results"]').click();
      refreshFacets();
      load(0);
    });
  });
  // A correction saved on the review screen shows here at once
  on("review-saved", (info) => {
    if (!r.data || r.preview) return;
    if (!info || !info.scan_id || info.scan_id === r.data.scan_id) open(r.data.scan_id, { keepView: true, quiet: true });
  });
  on("results-deleted", () => {
    r.prefetched.clear();
    load(r.offset, true);
  });
  on("results-scan", (scanId) => {
    document.querySelector('.tabs button[data-tab="results"]').click();
    open(scanId);
  });
}

export function onShowResults() {
  sizeCanvas();
  draw();
  if (!r.items.length) {
    refreshJobs();
    refreshFacets();
    load(0);
  }
}

function isActive() {
  return $("tab-results").classList.contains("active");
}

// ---------------------------------------------------------------------------
// listing
// ---------------------------------------------------------------------------
function currentFilters() {
  const f = {
    template_id: $("res-template").value,
    job_id: $("res-job").value,
    view: $("res-view").value,
    name: $("res-name").value,
    flag: $("res-flag").value,
    file: $("res-file").value.trim(),
  };
  for (const k of Object.keys(f)) if (!f[k]) delete f[k];
  return f;
}

async function refreshJobs() {
  try {
    const data = await api("/jobs?limit=200");
    const select = $("res-job");
    const current = select.value;
    select.innerHTML = "";
    select.append(el("option", { value: "" }, "All jobs"));
    for (const job of data.jobs) {
      select.append(el("option", { value: job.id }, `${job.name || job.id.slice(0, 8)} (${job.processed_files || 0})`));
    }
    if ([...select.options].some((o) => o.value === current)) select.value = current;
  } catch (e) {
    /* ignore */
  }
}

async function refreshFacets() {
  const params = new URLSearchParams();
  if ($("res-template").value) params.set("template_id", $("res-template").value);
  if ($("res-job").value) params.set("job_id", $("res-job").value);
  try {
    const facets = await api(`/results/facets?${params}`);
    const fill = (select, rows, key, label) => {
      const current = select.value;
      select.innerHTML = "";
      select.append(el("option", { value: "" }, label));
      for (const row of rows) select.append(el("option", { value: row[key] }, `${row[key]} (${row.n})`));
      if ([...select.options].some((o) => o.value === current)) select.value = current;
    };
    fill($("res-name"), facets.names, "name", "Any field");
    fill($("res-flag"), facets.flags, "flag", "Any flag");
    const views = facets.views || {};
    for (const option of $("res-view").options) {
      const n = views[option.value];
      option.textContent = option.dataset.label + (n === undefined ? "" : ` (${n})`);
    }
  } catch (e) {
    /* ignore */
  }
}

async function load(offset, keepPos) {
  r.filters = currentFilters();
  const params = new URLSearchParams({ ...r.filters, limit: String(PAGE), offset: String(offset) });
  try {
    const data = await api(`/results?${params}`);
    r.items = data.items;
    r.total = data.total;
    r.offset = offset;
  } catch (error) {
    toast(error.message, "error");
    return false;
  }
  renderList();
  if (keepPos === "first" && r.items.length) await openAt(0);
  else if (keepPos === "last" && r.items.length) await openAt(r.items.length - 1);
  else if (!keepPos && r.items.length && (!r.data || !r.items.some((i) => i.id === r.data.scan_id))) await openAt(0);
  else if (!r.items.length) {
    r.data = null;
    r.image = null;
    r.pos = -1;
    renderSide();
    draw();
  }
  return true;
}

function renderList() {
  const list = $("res-items");
  list.innerHTML = "";
  r.items.forEach((item, index) => {
    const selected = r.data && item.id === r.data.scan_id;
    if (selected) r.pos = index;
    list.append(
      el(
        "li",
        { class: selected ? "selected" : "", onclick: () => openAt(index), title: item.file_name },
        el("span", { class: "name" }, item.file_name || item.id.slice(0, 8)),
        item.flag_count && !item.verified ? el("span", { class: "chip flag", title: "fields flagged when read" }, `⚑${item.flag_count}`) : null,
        item.corrected ? el("span", { class: "chip corrected", title: "corrected" }, "✎") : null,
        item.verified ? el("span", { class: "chip ok", title: item.flag_count ? `verified (${item.flag_count} field(s) were flagged when read)` : "verified" }, "✓") : null,
        item.status === "error" ? chip("error", "error") : null,
        !item.verified && item.status === "needs_review" ? el("span", { class: "dot-flag", title: "needs review" }) : null
      )
    );
  });
  const end = r.offset + r.items.length;
  $("res-count").textContent = r.total ? `${r.offset + 1}–${end} of ${r.total}` : "No sheets match";
  $("res-prev-page").disabled = r.offset === 0;
  $("res-next-page").disabled = end >= r.total;
  const selectedNode = list.querySelector("li.selected");
  if (selectedNode && selectedNode.scrollIntoView) selectedNode.scrollIntoView({ block: "nearest" });
}

async function openAt(index) {
  const item = r.items[index];
  if (!item) return;
  r.pos = index;
  await open(item.id);
}

async function step(delta) {
  if (r.busy) return;
  const next = r.pos + delta;
  if (next >= 0 && next < r.items.length) return openAt(next);
  if (delta > 0 && r.offset + r.items.length < r.total) return load(r.offset + PAGE, "first");
  if (delta < 0 && r.offset > 0) return load(Math.max(0, r.offset - PAGE), "last");
  toast(delta > 0 ? "Last sheet" : "First sheet", "", 1200);
}

// ---------------------------------------------------------------------------
// one sheet
// ---------------------------------------------------------------------------
function fresh(path) {
  return url(`${path}${path.includes("?") ? "&" : "?"}t=${Date.now()}`);
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("Image could not be loaded"));
    img.src = src;
  });
}

async function open(scanId, { keepView = false, quiet = false } = {}) {
  if (r.align && r.align.scanId !== scanId) r.align = null;
  r.busy = true;
  if (!quiet) statusLine.textContent = "Rendering…";
  r.preview = null;
  if (!keepView) r.views = {};
  r.opening = scanId;
  try {
    const data = await api(`/scans/${scanId}/render`);
    const image = await loadImage(fresh(data.image_url));
    // Stepped on before this sheet arrived: the newer sheet wins
    if (r.opening !== scanId) return;
    const sameSize = r.image && r.image.naturalWidth === image.naturalWidth && r.image.naturalHeight === image.naturalHeight;
    const sheetChanged = !r.data || r.data.scan_id !== data.scan_id;
    r.data = data;
    r.image = image;
    // On another sheet the selection carries over only to a field that needs a
    // look there too; otherwise the last sheet's highlight would follow along
    const kept = r.selected && findItem(r.selected);
    if (!kept || (sheetChanged && !kept.flagged && !kept.pending)) r.selected = null;
    if (sheetChanged) r.scan = null;
    if (!keepView && !sameSize) fit();
    renderList();
    renderSide();
    draw();
    loadViews();
    prefetch();
  } catch (error) {
    if (r.opening !== scanId) return;
    // Show the values even when no image can be produced
    try {
      r.data = await api(`/scans/${scanId}/overlay`);
      r.data.warnings = [error.message];
      r.image = null;
      if (r.data.status === "error" && r.mode === "aligned" && !r.side) {
        // Nothing aligned to show: the original is what can be completed
        r.mode = "original";
        syncViewControls();
        loadViews();
      }
      renderList();
      renderSide();
      draw();
    } catch (e) {
      toast(error.message, "error", 6000);
    }
  } finally {
    if (r.opening === scanId) {
      r.busy = false;
      updateStatus();
    }
  }
}

function prefetch() {
  const next = r.items[r.pos + 1];
  if (!next || r.prefetched.has(next.id)) return;
  r.prefetched.add(next.id);
  // Warms the server's render cache (the re-read and the encoded image)
  api(`/scans/${next.id}/render`).catch(() => {});
}

function findItem(name) {
  if (!r.data) return null;
  const d = r.preview ? r.preview.data : r.data;
  return d.fields.find((f) => f.name === name) || d.zones.find((z) => z.name === name) || null;
}

function applyPayload(data) {
  const item = r.items.find((i) => i.id === data.scan_id);
  if (item) {
    item.verified = data.verified ? 1 : 0;
    item.corrected = data.corrected ? 1 : 0;
    item.status = data.status;
  }
  // The answer to a correction made on a sheet the user has since left: only
  // its list row is updated, never the sheet now on screen
  if (!r.data || r.data.scan_id !== data.scan_id) {
    renderList();
    return;
  }
  const keep = { image_url: r.data.image_url, width: r.data.width, height: r.data.height, image_source: r.data.image_source, warnings: r.data.warnings, drift: r.data.drift, resolved_path: r.data.resolved_path };
  r.data = Object.assign(data, keep);
  // An accepted item is done: drop its highlight
  const selected = r.selected && findItem(r.selected);
  if (selected && !selected.flagged && !selected.pending) r.selected = null;
  renderList();
  renderSide();
  draw();
}

async function correct(body) {
  if (!r.data || r.preview) return;
  try {
    const scanId = r.data.scan_id;
    const data = await api(`/scans/${scanId}/corrections`, { method: "POST", json: body });
    applyPayload(data);
    emit("review-changed");
    return true;
  } catch (error) {
    toast(error.message, "error", 5000);
    return false;
  }
}

async function verify() {
  if (!r.data || r.preview) return;
  const undo = !!r.data.verified;
  try {
    const data = await api(`/scans/${r.data.scan_id}/verify`, { method: undo ? "DELETE" : "POST" });
    applyPayload(data);
    emit("review-changed");
    if (!undo) {
      toast(`Verified${data.training_records ? ` · ${data.training_records} training samples` : ""}`, "ok", 1200);
      if (r.autoAdvance) step(1);
    }
  } catch (error) {
    toast(error.message, "error", 5000);
  }
}

// ---------------------------------------------------------------------------
// canvas: zoom/pan, overlay, clicks
// ---------------------------------------------------------------------------
function sizeCanvas() {
  const wrap = canvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  if (!w || !h) return;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  canvas.style.width = `${w}px`;
  canvas.style.height = `${h}px`;
}

function imageSize() {
  const img = r.preview ? r.preview.image : r.image;
  if (img) return [img.naturalWidth, img.naturalHeight];
  if (r.data && r.data.width) return [r.data.width, r.data.height];
  return [1000, 1400];
}

// ---- views: aligned (default), aligned in full colour, original as background,
// and side by side. All replay the geometry the engine recorded.
const VIEW_NAMES = { aligned: "Aligned", color: "Full colour", original: "Original" };

function viewEntry(kind) {
  const entry = r.views[kind];
  return entry && r.data && entry.scanId === r.data.scan_id ? entry : null;
}

// The panes on the canvas: [{kind, x0, w, k}], k = screen scale of a pane
// pixel relative to an aligned pixel (the original scan has its own size).
function panes() {
  const wrap = canvas.parentElement;
  const W = wrap.clientWidth || 1;
  const kinds = r.preview || !r.data ? ["aligned"] : r.side ? [r.left, r.mode] : [r.mode];
  const w = W / kinds.length;
  return kinds.map((kind, index) => ({ kind, x0: index * w, w, k: paneScale(kind) }));
}

function paneScale(kind) {
  if (kind !== "original") return 1;
  const entry = viewEntry("original");
  if (!entry || !entry.meta || !entry.meta.width) return 1;
  return imageSize()[0] / entry.meta.width;
}

function paneAt(clientX) {
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const list = panes();
  return list.find((p) => x >= p.x0 && x < p.x0 + p.w) || list[list.length - 1];
}

function paneImage(kind) {
  if (kind === "aligned") return r.preview ? r.preview.image : r.image;
  const entry = viewEntry(kind);
  return entry ? entry.image : null;
}

async function loadViews() {
  if (!r.data || r.preview) return;
  const scanId = r.data.scan_id;
  const needed = new Set(r.side ? [r.left, r.mode] : [r.mode]);
  for (const kind of ["original", "color"]) {
    if (!needed.has(kind) || viewEntry(kind)) continue;
    const entry = { scanId, loading: true, meta: null, image: null, error: null };
    r.views[kind] = entry;
    try {
      entry.meta = await api(`/scans/${scanId}/views/${kind}`);
      entry.image = await loadImage(url(entry.meta.image_url));
    } catch (error) {
      entry.error = error.message.replace(/^\d+: /, "");
    }
    entry.loading = false;
    if (r.data && r.data.scan_id === scanId) {
      draw();
      renderViewNotes();
    }
  }
}

function renderViewNotes() {
  const box = $("res-view-notes");
  if (!box) return;
  box.innerHTML = "";
  const kinds = r.preview || !r.data ? [] : r.side ? [r.left, r.mode] : [r.mode];
  for (const kind of new Set(kinds)) {
    const entry = viewEntry(kind);
    if (!entry) continue;
    if (entry.error) box.append(el("div", { class: "res-warning" }, `${VIEW_NAMES[kind]} view: ${entry.error}`));
    for (const warning of (entry.meta && entry.meta.warnings) || []) box.append(el("div", { class: "res-warning" }, `${VIEW_NAMES[kind]} view: ${warning}`));
  }
}

// Outlines of one pane: quads (TL, TR, BR, BL) in that pane's pixels
function rectQuad(x, y, w, h) {
  return [
    [x, y],
    [x + w, y],
    [x + w, y + h],
    [x, y + h],
  ];
}

function geometryFor(kind) {
  if (kind === "original") {
    const entry = viewEntry("original");
    const map = entry && entry.meta && entry.meta.map;
    if (!map) return null;
    return {
      bubble: (field, b) => (map.fields[field.name] && map.fields[field.name].bubbles[b.value]) || null,
      field: (field) => (map.fields[field.name] && map.fields[field.name].box) || null,
      zone: (zone) => map.zones[zone.name] || null,
      blocks: () => map.blocks || {},
    };
  }
  return {
    bubble: (field, b) => rectQuad(b.x, b.y, b.w, b.h),
    field: (field) => (field.box ? rectQuad(...field.box) : null),
    zone: (zone) => (zone.box && zone.box.length === 4 ? rectQuad(...zone.box) : null),
    blocks: () => (r.data && r.data.blocks) || {},
  };
}

function quadBounds(q) {
  const xs = q.map((p) => p[0]);
  const ys = q.map((p) => p[1]);
  return { x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) };
}

// Move each corner d pixels away from the centre (outlines around a box)
function grow(q, d) {
  const cx = q.reduce((s, p) => s + p[0], 0) / q.length;
  const cy = q.reduce((s, p) => s + p[1], 0) / q.length;
  return q.map(([x, y]) => [x + Math.sign(x - cx) * d, y + Math.sign(y - cy) * d]);
}

function inQuad(p, q) {
  let inside = false;
  for (let i = 0, j = q.length - 1; i < q.length; j = i++) {
    const [xi, yi] = q[i];
    const [xj, yj] = q[j];
    if (yi > p.y !== yj > p.y && p.x < ((xj - xi) * (p.y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function fit() {
  const wrap = canvas.parentElement;
  const pane = panes()[0];
  const [w, h] = imageSize();
  const scale = Math.min((pane.w - 20) / w, (wrap.clientHeight - 20) / h);
  r.view.scale = scale > 0 ? scale : 1;
  r.view.x = (pane.w - w * r.view.scale) / 2;
  r.view.y = (wrap.clientHeight - h * r.view.scale) / 2;
}

// cx, cy relative to the pane's top-left corner; zoom and scroll are shared
// by every pane, so side-by-side panes move together
function zoom(factor, cx, cy) {
  const wrap = canvas.parentElement;
  if (cx === undefined) {
    cx = panes()[0].w / 2;
    cy = wrap.clientHeight / 2;
  }
  const scale = Math.max(0.05, Math.min(12, r.view.scale * factor));
  const k = scale / r.view.scale;
  r.view.x = cx - (cx - r.view.x) * k;
  r.view.y = cy - (cy - r.view.y) * k;
  r.view.scale = scale;
  draw();
}

function toImage(clientX, clientY, pane) {
  const rect = canvas.getBoundingClientRect();
  pane = pane || paneAt(clientX);
  const s = r.view.scale * pane.k;
  return { x: (clientX - rect.left - pane.x0 - r.view.x) / s, y: (clientY - rect.top - r.view.y) / s, pane };
}

function hitTest(p) {
  if (!r.data || !r.overlay) return null;
  const d = r.preview ? r.preview.data : r.data;
  const geo = geometryFor(p.pane ? p.pane.kind : "aligned");
  if (!geo) return null;
  const pad = 2 / ((p.pane && p.pane.k) || 1);
  for (const field of d.fields) {
    const box = geo.field(field);
    if (!box || !inQuad(p, grow(box, pad))) continue;
    for (const b of field.bubbles) {
      const q = geo.bubble(field, b);
      if (q && inQuad(p, grow(q, pad))) return { kind: "bubble", field, bubble: b };
    }
    return { kind: "field", field };
  }
  for (const zone of d.zones) {
    const q = geo.zone(zone);
    if (q && inQuad(p, q)) return { kind: "zone", zone };
  }
  return null;
}

function bindCanvas() {
  let drag = null;
  canvas.addEventListener("mousedown", (e) => {
    if (r.scan) {
      // Scanning a code: the drag draws the box instead of panning
      const p = toImage(e.clientX, e.clientY);
      drag = { scan: true, pane: p.pane };
      r.scan.box = { kind: p.pane.kind, x0: p.x, y0: p.y, x1: p.x, y1: p.y };
      return;
    }
    drag = { x: e.clientX, y: e.clientY, vx: r.view.x, vy: r.view.y, moved: false };
  });
  window.addEventListener("mousemove", (e) => {
    if (drag && drag.scan) {
      const p = toImage(e.clientX, e.clientY, drag.pane);
      r.scan.box.x1 = p.x;
      r.scan.box.y1 = p.y;
      draw();
      return;
    }
    if (drag) {
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
      if (drag.moved) {
        r.view.x = drag.vx + dx;
        r.view.y = drag.vy + dy;
        draw();
      }
      return;
    }
    if (e.target !== canvas) return;
    const hit = hitTest(toImage(e.clientX, e.clientY));
    const key = hit ? (hit.bubble ? `${hit.field.name}/${hit.bubble.value}` : (hit.field || hit.zone).name) : null;
    canvas.style.cursor = r.scan ? "crosshair" : hit ? "pointer" : "grab";
    // The same key highlights the spot on every pane
    if (key !== r.hover) {
      r.hover = key;
      r.hoverHit = hit;
      draw();
      updateStatus();
    }
  });
  window.addEventListener("mouseup", (e) => {
    const was = drag;
    drag = null;
    if (was && was.scan) return decodeBox();
    if (!was || was.moved || e.target !== canvas) return;
    if (r.align) return alignClick(toImage(e.clientX, e.clientY));
    const hit = hitTest(toImage(e.clientX, e.clientY));
    if (!hit) return;
    if (hit.kind === "bubble") {
      r.selected = hit.field.name;
      if (r.preview) return draw();
      correct({ toggle: [{ field: hit.field.name, value: hit.bubble.value }] });
    } else {
      select((hit.field || hit.zone).name, true);
    }
  });
  canvas.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const pane = paneAt(e.clientX);
      zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - rect.left - pane.x0, e.clientY - rect.top);
    },
    { passive: false }
  );
  canvas.addEventListener("dblclick", (e) => e.preventDefault());
}

const COLORS = {
  marked: "rgba(47, 111, 223, 0.35)",
  markedLine: "#2f6fdf",
  empty: "rgba(110, 118, 135, 0.55)",
  flagged: "#e08a00",
  group: "#c2185b",
  corrected: "#8e44ad",
  selected: "#d0342c",
  hover: "#00a3a3",
  zone: { barcode: "#c85a14", qrcode: "#a028a0", ocr: "#1e8c1e", icr: "#1478dc" },
  block: { found: "#1e8c1e", fitted: "#1478dc", failed: "#d0342c", skipped: "#8a909c" },
};

function draw() {
  if (!ctx2d) return;
  const dpr = window.devicePixelRatio || 1;
  ctx2d.setTransform(1, 0, 0, 1, 0, 0);
  ctx2d.clearRect(0, 0, canvas.width, canvas.height);
  const list = panes();
  for (const pane of list) {
    ctx2d.save();
    ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx2d.beginPath();
    ctx2d.rect(pane.x0, 0, pane.w, canvas.height / dpr);
    ctx2d.clip();
    drawPane(pane, dpr);
    ctx2d.restore();
  }
  if (list.length > 1) {
    ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx2d.fillStyle = "#2b2f38";
    for (const pane of list.slice(1)) ctx2d.fillRect(pane.x0 - 1, 0, 2, canvas.height / dpr);
    ctx2d.font = "12px system-ui, sans-serif";
    for (const pane of list) {
      const text = VIEW_NAMES[pane.kind];
      const width = ctx2d.measureText(text).width;
      ctx2d.fillStyle = "rgba(43, 47, 56, 0.8)";
      ctx2d.fillRect(pane.x0 + 6, 6, width + 10, 18);
      ctx2d.fillStyle = "#fff";
      ctx2d.fillText(text, pane.x0 + 11, 19);
    }
  }
}

function drawPane(pane, dpr) {
  const s = r.view.scale * pane.k;
  ctx2d.setTransform(dpr * s, 0, 0, dpr * s, dpr * (pane.x0 + r.view.x), dpr * r.view.y);
  const img = paneImage(pane.kind);
  const entry = pane.kind === "aligned" ? null : viewEntry(pane.kind);
  let [w, h] = imageSize();
  if (img) [w, h] = [img.naturalWidth, img.naturalHeight];
  else if (entry && entry.meta) [w, h] = [entry.meta.width, entry.meta.height];
  ctx2d.fillStyle = "#fff";
  ctx2d.fillRect(0, 0, w, h);
  const px = 1 / s; // one screen pixel in pane pixels
  if (img) {
    ctx2d.imageSmoothingEnabled = s < 2;
    ctx2d.drawImage(img, 0, 0);
  } else if (pane.kind !== "aligned") {
    const text = !entry || entry.loading ? `Loading the ${VIEW_NAMES[pane.kind].toLowerCase()} view…` : `${VIEW_NAMES[pane.kind]} view unavailable: ${entry.error || ""}`;
    ctx2d.fillStyle = "#555";
    ctx2d.font = `${14 * px}px system-ui, sans-serif`;
    ctx2d.fillText(text, 20 * px, 30 * px, w - 40 * px);
    return;
  }
  if (r.align && pane.kind === "original") drawAlignClicks(px);
  if (pane.kind === "original" && r.borders) drawPageOutline(px);
  if (r.scan && r.scan.box && r.scan.box.kind === pane.kind) drawScanBox(r.scan.box, px);
  if (!r.data) return;
  const geo = geometryFor(pane.kind);
  if (!geo) return;
  if (r.borders) drawBlocks(geo, px);
  if (!r.overlay) return;
  const d = r.preview ? r.preview.data : r.data;
  for (const field of d.fields) drawField(field, px, geo);
  for (const zone of d.zones) drawZone(zone, px, geo);
}

function path(q) {
  ctx2d.beginPath();
  q.forEach(([x, y], i) => (i ? ctx2d.lineTo(x, y) : ctx2d.moveTo(x, y)));
  ctx2d.closePath();
}

// Item 29: block borders the engine found / fitted (show or hide)
function drawBlocks(geo, px) {
  for (const [name, block] of Object.entries(geo.blocks())) {
    const q = block && block.corners;
    if (!q || q.length !== 4) continue;
    ctx2d.strokeStyle = COLORS.block[block.status] || "#555";
    ctx2d.lineWidth = 2 * px;
    ctx2d.setLineDash(block.status === "found" ? [] : [8 * px, 5 * px]);
    path(q);
    ctx2d.stroke();
    ctx2d.setLineDash([]);
    const b = quadBounds(q);
    label(`${name}${block.status ? ` (${block.status})` : ""}`, b.x0, b.y1 + 14 * px, px, ctx2d.strokeStyle);
  }
}

function drawField(field, px, geo) {
  const selected = field.name === r.selected;
  const grouped = (field.group_flags || []).length > 0;
  for (const b of field.bubbles) {
    const q = geo.bubble(field, b);
    if (!q) continue;
    const hovered = r.hover === `${field.name}/${b.value}`;
    path(q);
    if (b.marked) {
      ctx2d.fillStyle = COLORS.marked;
      ctx2d.fill();
    }
    ctx2d.lineWidth = (b.marked ? 2 : 1) * px * (hovered ? 2 : 1);
    ctx2d.strokeStyle = hovered ? COLORS.hover : b.marked ? COLORS.markedLine : COLORS.empty;
    if (field.flagged && !hovered) ctx2d.strokeStyle = b.marked ? COLORS.markedLine : COLORS.flagged;
    ctx2d.stroke();
    if (b.marked !== b.read_marked) {
      // A person changed this bubble: mark the difference to the machine read
      const bb = quadBounds(q);
      ctx2d.strokeStyle = COLORS.corrected;
      ctx2d.lineWidth = 2.5 * px;
      ctx2d.beginPath();
      ctx2d.arc((bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2, Math.max(bb.x1 - bb.x0, bb.y1 - bb.y0) * 0.62, 0, Math.PI * 2);
      ctx2d.stroke();
    }
  }
  const box = geo.field(field);
  if (!box) return;
  const hovered = r.hover === field.name;
  if (field.flagged || selected || field.corrected || grouped || hovered) {
    ctx2d.lineWidth = (selected ? 3 : 2) * px;
    ctx2d.strokeStyle = selected ? COLORS.selected : hovered ? COLORS.hover : field.corrected ? COLORS.corrected : grouped ? COLORS.group : COLORS.flagged;
    if (field.flagged && !selected && field.pending) ctx2d.setLineDash([6 * px, 4 * px]);
    path(grow(box, 4 * px));
    ctx2d.stroke();
    ctx2d.setLineDash([]);
  }
  const b = quadBounds(box);
  if (selected || field.flagged || grouped || r.view.scale > 0.9) label(`${field.name}: ${field.value === "" ? "∅" : field.value}`, b.x0, b.y0 - 6 * px, px, selected ? COLORS.selected : grouped ? COLORS.group : field.flagged ? COLORS.flagged : "#333");
}

function drawZone(zone, px, geo) {
  const q = geo.zone(zone);
  if (!q) return;
  const selected = zone.name === r.selected;
  const hovered = r.hover === zone.name;
  ctx2d.lineWidth = (selected || hovered ? 3 : 2) * px;
  ctx2d.strokeStyle = selected ? COLORS.selected : hovered ? COLORS.hover : zone.flagged ? COLORS.flagged : zone.corrected ? COLORS.corrected : COLORS.zone[zone.type] || "#333";
  path(q);
  ctx2d.stroke();
  const b = quadBounds(q);
  label(`${zone.name}: ${zone.value === "" ? "∅" : zone.value}`, b.x0, b.y0 - 6 * px, px, ctx2d.strokeStyle);
}

function label(text, x, y, px, color) {
  const size = 12 * px;
  ctx2d.font = `${size}px system-ui, sans-serif`;
  const width = ctx2d.measureText(text).width;
  ctx2d.fillStyle = "rgba(255,255,255,0.85)";
  ctx2d.fillRect(x - 2 * px, y - size, width + 4 * px, size + 3 * px);
  ctx2d.fillStyle = color;
  ctx2d.fillText(text, x, y);
}

function updateStatus() {
  if (!statusLine) return;
  const parts = [`zoom ${Math.round(r.view.scale * 100)}%`];
  const hit = r.hoverHit;
  if (hit && r.hover) {
    if (hit.bubble) parts.push(`${hit.field.name} · ${hit.bubble.value} · fill ${Math.round((hit.bubble.fill_ratio || 0) * 100)}% · click to ${hit.bubble.marked ? "unmark" : "mark"}`);
    else parts.push((hit.field || hit.zone).name);
    const flags = hit.field && hit.field.group_flags;
    if (flags && flags.length) parts.push(flags.join("; "));
  }
  if (r.preview) parts.push("REGRADE PREVIEW (not saved)");
  if (r.scan) parts.push(`SCAN ${r.scan.name}: drag a box around the code (Esc cancels)`);
  statusLine.textContent = parts.join("  ·  ");
}

function centerOn(item) {
  const box = item.box;
  if (!box) return;
  const wrap = canvas.parentElement;
  const pane = panes().find((p) => p.kind !== "original") || panes()[0];
  if (pane.kind === "original") return;
  const sx = r.view.x + box[0] * r.view.scale;
  const sy = r.view.y + box[1] * r.view.scale;
  const ex = sx + box[2] * r.view.scale;
  const ey = sy + box[3] * r.view.scale;
  if (sx >= 0 && sy >= 0 && ex <= pane.w && ey <= wrap.clientHeight) return;
  r.view.x = pane.w / 2 - (box[0] + box[2] / 2) * r.view.scale;
  r.view.y = wrap.clientHeight / 2 - (box[1] + box[3] / 2) * r.view.scale;
}

function select(name, focusInput) {
  r.selected = name;
  const item = findItem(name);
  if (item) centerOn(item);
  draw();
  for (const row of document.querySelectorAll("#res-side .res-row")) {
    const match = row.dataset.names && row.dataset.names.split("\n").includes(name);
    row.classList.toggle("selected", !!match);
    if (match) {
      const details = row.closest("details");
      if (details) details.open = true;
      if (row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
      const input = row.querySelector(`input[data-name="${CSS.escape(name)}"]`);
      if (focusInput && input) {
        input.focus();
        input.select();
      }
    }
  }
}

// ---------------------------------------------------------------------------
// sidebar
// ---------------------------------------------------------------------------
function confText(conf) {
  return conf === null || conf === undefined ? "–" : Number(conf).toFixed(2);
}

function valueInput(item) {
  const input = el("input", { class: "res-input mono", value: item.value, "data-name": item.name, spellcheck: "false", autocomplete: "off", title: item.original_value !== item.value ? `read as: ${item.original_value === "" ? "(blank)" : item.original_value}` : "" });
  if (r.preview) input.disabled = true;
  const commit = async () => {
    const value = item.kind === "field" ? input.value.trim().toUpperCase() : input.value;
    if (value === item.value) return;
    const ok = await correct({ changes: { [item.name]: value } });
    if (!ok) {
      input.classList.add("invalid");
      input.value = item.value;
    }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      input.value = item.value;
      input.blur();
    }
  });
  input.addEventListener("change", commit);
  input.addEventListener("focus", () => {
    r.selected = item.name;
    draw();
  });
  return input;
}

function acceptButton(item) {
  if (!(item.pending || (item.acceptAlso || []).length) || r.preview) return null;
  return el(
    "button",
    {
      class: "small ghost res-accept",
      title: "The value is right: settle this item",
      onclick: (e) => {
        e.preventDefault();
        e.stopPropagation();
        // A grouped value settles its columns too
        correct({ accept: [item.check || item.name, ...(item.acceptAlso || [])] });
      },
    },
    "Accept"
  );
}

// Drag to scan: barcodes and QR codes can't be read by eye, so the reviewer
// draws a box around the code and the server decodes the original there
function scanButton(item) {
  if (!["barcode", "qrcode"].includes(item.type) || r.preview) return null;
  const active = r.scan && r.scan.name === item.name;
  return el(
    "button",
    {
      class: `small${active ? " primary" : ""}`,
      title: "Drag a box around the code on the sheet to decode it",
      onclick: (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (active) return stopScan();
        r.scan = { name: item.name, box: null };
        canvas.style.cursor = "crosshair";
        renderSide();
        updateStatus();
      },
    },
    active ? "Cancel scan" : "Scan"
  );
}

function stopScan() {
  r.scan = null;
  canvas.style.cursor = "grab";
  renderSide();
  draw();
  updateStatus();
}

function drawScanBox(box, px) {
  ctx2d.save();
  ctx2d.strokeStyle = "#e0218a";
  ctx2d.lineWidth = 2 * px;
  ctx2d.setLineDash([6 * px, 4 * px]);
  ctx2d.strokeRect(Math.min(box.x0, box.x1), Math.min(box.y0, box.y1), Math.abs(box.x1 - box.x0), Math.abs(box.y1 - box.y0));
  ctx2d.restore();
}

async function decodeBox() {
  const scan = r.scan;
  const box = scan && scan.box;
  if (!box || !r.data) return;
  const x = Math.min(box.x0, box.x1);
  const y = Math.min(box.y0, box.y1);
  const w = Math.abs(box.x1 - box.x0);
  const h = Math.abs(box.y1 - box.y0);
  if (w < 4 || h < 4) {
    scan.box = null;
    return draw();
  }
  const scanId = r.data.scan_id;
  statusLine.textContent = "Decoding…";
  try {
    const found = await api(`/scans/${scanId}/decode`, { method: "POST", json: { box: [x, y, w, h], view: box.kind === "original" ? "original" : "aligned", zone: scan.name } });
    if (!r.data || r.data.scan_id !== scanId || r.scan !== scan) return;
    if (!found.value) {
      scan.box = null;
      draw();
      updateStatus();
      return toast("No code found in that box. Try a box with a little white around the code.", "error", 5000);
    }
    const others = (found.symbols || []).length - 1;
    stopScan();
    r.selected = scan.name;
    if (await correct({ changes: { [scan.name]: found.value } })) {
      toast(`${scan.name}: ${found.value}${found.format ? ` (${found.format})` : ""}${others > 0 ? ` · ${others} more code(s) in the box` : ""}`, "ok", 5000);
    }
  } catch (error) {
    scan.box = null;
    draw();
    updateStatus();
    toast(error.message, "error", 6000);
  }
}

function itemRow(item, indent) {
  const label = item.kind === "check" ? "check" : item.kind === "custom_label" ? "label" : null;
  return el(
    "div",
    { class: `res-row${item.flagged ? " flagged" : ""}${item.corrected ? " corrected" : ""}${indent ? " part" : ""}${item.name === r.selected ? " selected" : ""}`, "data-names": item.name, onclick: (e) => { if (!typing(e.target)) select(item.name); } },
    el("span", { class: "res-name", title: item.name }, item.name, label ? el("span", { class: "muted small" }, ` ${label}`) : null),
    valueInput(item),
    el("span", { class: "conf", style: { background: confidenceColor(item.confidence) } }, confText(item.confidence)),
    el(
      "div",
      { class: "res-flags" },
      acceptButton(item),
      scanButton(item),
      item.corrected ? el("span", { class: "chip corrected" }, `was ${item.original_value === "" || item.original_value === undefined ? "∅" : item.original_value}`) : null,
      (item.flags || []).map((f) => chip(f, "flag")),
      (item.group_flags || []).map((f) => el("span", { class: "chip group-flag", title: "Column of a grouped value that needs a look" }, f)),
      (item.reasons || []).length ? el("span", { class: "muted small res-reasons" }, item.reasons.join("; ")) : null
    )
  );
}

function renderSide() {
  const side = $("res-side");
  side.innerHTML = "";
  const d = r.preview ? r.preview.data : r.data;
  if (!d) {
    side.append(el("div", { class: "empty-state" }, "No sheet selected"));
    return;
  }
  const verified = d.verified;
  side.append(
    el("div", { class: "res-head" }, el("strong", { class: "res-file", title: d.source_path || "" }, d.file_id || d.scan_id), el("div", { class: "row gap wrap" }, chip(d.status, d.status), d.corrected ? el("span", { class: "chip corrected" }, "corrected") : null, verified ? el("span", { class: "chip ok" }, `verified by ${verified.by}`) : el("span", { class: "chip" }, "not verified"), d.score !== null && d.score !== undefined ? el("span", { class: "chip" }, `score ${d.score}`) : null)),
    el("div", { class: "muted small res-path" }, d.resolved_path || d.source_path || ""),
    registrationSummary(d),
    (d.duplicates || []).length
      ? el(
          "div",
          { class: "chip error", title: "Another sheet of this job has the same primary key" },
          "Duplicate key: also on ",
          ...d.duplicates.map((dup) => el("button", { class: "small ghost", onclick: () => open(dup.scan_id) }, dup.file_name || dup.scan_id))
        )
      : "",
    scoreSummary(d.scoring) || ""
  );
  for (const warning of r.data.warnings || []) side.append(el("div", { class: "res-warning" }, warning));
  side.append(el("div", { id: "res-view-notes" }));
  if (r.data.drift && r.data.drift.length) side.append(el("div", { class: "res-warning" }, `Re-reading now gives different values for: ${r.data.drift.join(", ")}`));
  if (d.error) side.append(el("div", { class: "res-warning error" }, d.error));
  if (d.status === "error" && !r.preview) side.append(failedSheetBox(d));
  if (r.preview) side.append(previewBox());
  for (const item of d.sheet_review || []) {
    const detail = item.marked_bubbles !== undefined ? ` (${item.marked_bubbles} marked, minimum ${item.min_marked_bubbles})` : "";
    side.append(el("div", { class: "res-warning" }, el("strong", {}, item.name), detail, " ", acceptButton({ name: item.name, pending: true })));
  }

  const byName = {};
  for (const item of [...d.fields, ...d.zones]) byName[item.name] = item;
  const outputs = d.outputs.slice().sort((a, b) => (b.flagged ? 1 : 0) - (a.flagged ? 1 : 0));
  const list = el("div", { class: "res-outputs" });
  for (const output of outputs) {
    if (output.parts) {
      const parts = output.parts.map((p) => byName[p]).filter(Boolean);
      const joined = valueInput({ ...output, kind: "custom_label" });
      joined.title = `Type the whole ${output.name}; it is split over ${output.parts.join(", ")} (space = blank column)`;
      // Typing in a <summary> must not open/close the group
      joined.addEventListener("click", (e) => e.preventDefault());
      joined.addEventListener("keyup", (e) => {
        if (e.key === " ") e.preventDefault();
      });
      const head = el("summary", { class: `res-row group${output.flagged ? " flagged" : ""}${output.corrected ? " corrected" : ""}`, "data-names": [output.name, ...output.parts].join("\n") }, el("span", { class: "res-name" }, output.name), joined, el("span", { class: "conf", style: { background: confidenceColor(output.confidence) } }, confText(output.confidence)), el("div", { class: "res-flags" }, acceptButton({ ...output, acceptAlso: parts.filter((p) => p.pending || p.flagged).map((p) => p.name) }), (output.flags || []).map((f) => chip(f, "flag")), (output.reasons || []).length ? el("span", { class: "muted small res-reasons" }, output.reasons.join("; ")) : null));
      const details = el("details", { class: "res-group" }, head, parts.map((p) => itemRow(p, true)));
      if (parts.some((p) => (p.group_flags || []).length)) head.classList.add("group-flagged");
      if (output.flagged || output.corrected || head.classList.contains("group-flagged")) details.open = true;
      list.append(details);
    } else if (byName[output.name]) {
      const row = itemRow(byName[output.name]);
      for (const f of output.flags || []) if (!(byName[output.name].flags || []).includes(f)) row.querySelector(".res-flags").append(chip(f, "flag"));
      list.append(row);
    } else if (output.editable) {
      list.append(itemRow(output));
    } else {
      list.append(el("div", { class: "res-row" }, el("span", { class: "res-name" }, output.name), el("span", { class: "mono" }, output.value)));
    }
  }
  side.append(el("h3", {}, `Fields (${outputs.filter((o) => o.flagged).length} flagged)`), list);

  const checks = Object.entries(d.checks || {});
  if (checks.length) {
    side.append(
      el("h3", {}, "Checks"),
      el(
        "div",
        { class: "res-outputs" },
        checks.map(([name, check]) => {
          const c = check && typeof check === "object" ? check : { value: check };
          const pendingCheck = (d.pending || []).includes(name);
          return el(
            "div",
            { class: `res-row${(c.flags || []).length ? " flagged" : ""}` },
            el("span", { class: "res-name" }, name),
            el("span", { class: "mono res-concat" }, c.value === undefined || c.value === null ? "" : String(c.value)),
            el("span", { class: "muted small" }, c.manual ? "typed by a person" : c.chosen_source ? `from ${c.chosen_source}` : ""),
            el(
              "div",
              { class: "res-flags" },
              acceptButton({ name, pending: pendingCheck }),
              (c.flags || []).map((f) => chip(f, "flag")),
              (c.validation_reasons || []).length ? el("span", { class: "muted small res-reasons" }, c.validation_reasons.join("; ")) : null
            ),
            c.sources ? el("details", { class: "small muted res-sources" }, el("summary", {}, "sources"), el("pre", {}, JSON.stringify(c.sources, null, 1))) : null
          );
        })
      )
    );
  }
  if ((d.audit || []).length) {
    side.append(
      el(
        "details",
        { class: "res-audit" },
        el("summary", {}, `Changes (${d.audit.length})`),
        el(
          "ul",
          {},
          d.audit.slice().reverse().map((a) => el("li", {}, el("code", {}, a.name), ` ${a.old === "" ? "∅" : a.old} → ${a.new === "" ? "∅" : a.new}`, el("span", { class: "muted" }, ` · ${a.user} · ${new Date(a.at * 1000).toLocaleString()}`)))
        )
      )
    );
  }
  renderViewNotes();
  if (d.regrade) side.append(el("div", { class: "muted small" }, `Regraded by ${d.regrade.by} with `, el("code", {}, JSON.stringify({ ...d.regrade.template_overrides, ...d.regrade.config_overrides }))));
  $("res-verify").textContent = verified ? "Unverify" : "Verify ✓";
  $("res-verify").classList.toggle("primary", !verified);
}

// ---------------------------------------------------------------------------
// keyboard
// ---------------------------------------------------------------------------
function onKey(e) {
  if (!isActive() || document.querySelector(".modal-backdrop")) return;
  if (typing(e.target)) {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  const k = e.key;
  if (k === "Escape" && r.scan) return stopScan();
  if (k === "j" || k === "ArrowDown" || k === "ArrowRight") step(1);
  else if (k === "k" || k === "ArrowUp" || k === "ArrowLeft") step(-1);
  else if (k === "v" || (k === "Enter" && !e.target.matches("button"))) verify();
  else if (k === "f") {
    fit();
    draw();
  } else if (k === "+" || k === "=") zoom(1.25);
  else if (k === "-") zoom(1 / 1.25);
  else if (k === "o") {
    r.overlay = !r.overlay;
    draw();
  } else if (k === "b") setViewOption("borders", !r.borders);
  else if (k === "s") setViewOption("side", !r.side);
  else if (k === "n" || k === "Tab") cycleFlagged(e.shiftKey ? -1 : 1);
  else if (k === "r") showRegrade();
  else if (k === "e" && r.selected) select(r.selected, true);
  else return;
  e.preventDefault();
}

function cycleFlagged(delta) {
  if (!r.data) return;
  const d = r.preview ? r.preview.data : r.data;
  const names = [...d.fields, ...d.zones].filter((i) => i.flagged).map((i) => i.name);
  if (!names.length) return;
  const index = names.indexOf(r.selected);
  select(names[(index + delta + names.length) % names.length]);
}

// ---------------------------------------------------------------------------
// toolbar dialogs: regrade, accuracy, path remap
// ---------------------------------------------------------------------------
export function bindToolbar() {
  $("res-prev").addEventListener("click", () => step(-1));
  $("res-next").addEventListener("click", () => step(1));
  $("res-verify").addEventListener("click", verify);
  $("res-fit").addEventListener("click", () => {
    fit();
    draw();
  });
  $("res-zoom-in").addEventListener("click", () => zoom(1.25));
  $("res-zoom-out").addEventListener("click", () => zoom(1 / 1.25));
  $("res-overlay").addEventListener("change", (e) => {
    r.overlay = e.target.checked;
    draw();
  });
  $("res-advance").checked = r.autoAdvance;
  $("res-advance").addEventListener("change", (e) => {
    r.autoAdvance = e.target.checked;
    safeStorage("set", "omr_res_advance", r.autoAdvance ? "1" : "0");
  });
  $("res-regrade").addEventListener("click", showRegrade);
  $("res-delete").addEventListener("click", deleteScan);
  // View control: aligned / full colour / original, side by side, borders
  $("res-mode").value = r.mode;
  $("res-left").value = r.left;
  $("res-sbs").checked = r.side;
  $("res-borders").checked = r.borders;
  $("res-mode").addEventListener("change", (e) => setViewOption("mode", e.target.value));
  $("res-left").addEventListener("change", (e) => setViewOption("left", e.target.value));
  $("res-sbs").addEventListener("change", (e) => setViewOption("side", e.target.checked));
  $("res-borders").addEventListener("change", (e) => setViewOption("borders", e.target.checked));
  syncViewControls();
}

function syncViewControls() {
  $("res-mode").value = r.mode;
  $("res-left").value = r.left;
  $("res-sbs").checked = r.side;
  $("res-borders").checked = r.borders;
  $("res-left-wrap").classList.toggle("hidden", !r.side);
}

function setViewOption(name, value) {
  const wasSide = r.side;
  r[name] = value;
  const stored = typeof value === "boolean" ? (value ? "1" : "0") : value;
  safeStorage("set", `omr_res_${name}`, stored);
  syncViewControls();
  if (name === "side" && wasSide !== r.side) fit();
  loadViews();
  renderViewNotes();
  draw();
}

// ---- failed sheets (item 25): align by clicking, or type the values
const CORNER_NAMES = ["top-left", "top-right", "bottom-right", "bottom-left"];

function failedSheetBox(d) {
  const box = el(
    "div",
    { class: "res-failed" },
    el("strong", {}, "This sheet could not be aligned."),
    el("p", { class: "muted small" }, "Click its page corners (or the template's index points) on the original image and it is read again, or type its values by hand.")
  );
  const startAlign = async (kind) => {
    try {
      const targets = await api(`/scans/${d.scan_id}/manual-align`);
      const names = kind === "corners" ? CORNER_NAMES : targets.index_points.map((p) => p.name);
      if (kind === "index" && names.length < 4) return toast("This template has fewer than 4 index points; click the page corners instead", "error", 5000);
      r.align = { kind, names, points: [], scanId: d.scan_id };
      if (r.mode !== "original") setViewOption("mode", "original");
      alignPrompt();
      draw();
    } catch (error) {
      toast(error.message, "error", 5000);
    }
  };
  box.append(
    el(
      "div",
      { class: "row gap wrap" },
      el("button", { class: "small primary", onclick: () => startAlign("corners") }, "Align by page corners"),
      el("button", { class: "small", onclick: () => startAlign("index") }, "Align by index points"),
      el("button", { class: "small", onclick: () => typeValues(d) }, "Type values…"),
      r.align ? el("button", { class: "small ghost", onclick: () => { r.align = null; draw(); renderSide(); } }, "Cancel clicking") : null
    )
  );
  if (r.align) box.append(el("div", { class: "chip" }, alignPromptText()));
  return box;
}

function alignPromptText() {
  const a = r.align;
  const next = a.names[a.points.length];
  return next ? `Click ${next} (${a.points.length + 1} of ${a.names.length}) on the original` : "Reading…";
}

function alignPrompt() {
  statusLine.textContent = alignPromptText();
  renderSide();
}

async function alignClick(p) {
  const a = r.align;
  if (!a || !r.data || r.data.scan_id !== a.scanId) return (r.align = null);
  if (p.pane.kind !== "original") return toast("Click on the Original view");
  // Clicks while the alignment is being sent are ignored
  if (a.points.length >= a.names.length) return;
  a.points.push([Math.round(p.x), Math.round(p.y)]);
  draw();
  if (a.points.length < a.names.length) return alignPrompt();
  alignPrompt();
  try {
    await api(`/scans/${a.scanId}/manual-align`, { method: "POST", json: { points: a.points, kind: a.kind, names: a.kind === "index" ? a.names : null } });
    toast("Aligned by hand and read again", "ok");
    r.align = null;
    await open(a.scanId);
    load(r.offset, true);
  } catch (error) {
    r.align = null;
    toast(error.message, "error", 6000);
    renderSide();
    draw();
  }
}

// Registration view: the aligned page's outline on the original scan
function drawPageOutline(px) {
  const entry = viewEntry("original");
  const q = entry && entry.meta && entry.meta.map && entry.meta.map.page;
  if (!q) return;
  ctx2d.strokeStyle = "#1478dc";
  ctx2d.lineWidth = 2 * px;
  ctx2d.setLineDash([10 * px, 6 * px]);
  path(q);
  ctx2d.stroke();
  ctx2d.setLineDash([]);
  // The page's top-left corner, so a sheet read upside down is obvious
  ctx2d.fillStyle = "#1478dc";
  ctx2d.beginPath();
  ctx2d.arc(q[0][0], q[0][1], 7 * px, 0, Math.PI * 2);
  ctx2d.fill();
  label("top-left", q[0][0] + 10 * px, q[0][1] + 16 * px, px, "#1478dc");
}

function registrationSummary(d) {
  const g = d.geometry;
  if (!g && !d.manual_alignment) return "";
  const parts = [];
  const method = (g && g.alignment_method) || "";
  if (d.manual_alignment) parts.push(`aligned by hand (${d.manual_alignment.kind === "index" ? "index points" : "page corners"})`);
  else if (method) parts.push(String(method).replace(/_/g, " "));
  if (g && g.rotation) parts.push(`turned ${g.rotation}°`);
  const residual = g && g.residual && typeof g.residual === "object" ? g.residual.page : g && g.residual;
  if (residual !== null && residual !== undefined && Number.isFinite(Number(residual))) parts.push(`residual ${Number(residual).toFixed(2)} px`);
  const points = (g && g.index_points) || [];
  if (points.length) parts.push(`index points ${points.filter((p) => p.found).length}/${points.length} found`);
  if (!parts.length) return "";
  return el("div", { class: "muted small", title: "How the page was registered. Turn on block borders and the Original view to see the page outline on the scan." }, `Registration: ${parts.join(" · ")}`);
}

function drawAlignClicks(px) {
  ctx2d.lineWidth = 2 * px;
  r.align.points.forEach(([x, y], i) => {
    ctx2d.strokeStyle = COLORS.selected;
    ctx2d.beginPath();
    ctx2d.arc(x, y, 8 * px, 0, Math.PI * 2);
    ctx2d.stroke();
    label(r.align.names[i], x + 10 * px, y - 10 * px, px, COLORS.selected);
  });
}

async function typeValues(d) {
  let targets;
  try {
    targets = await api(`/scans/${d.scan_id}/manual-align`);
  } catch (error) {
    return toast(error.message, "error", 5000);
  }
  const inputs = {};
  const rows = (targets.output_columns || []).map((name) => {
    inputs[name] = el("input", { value: (d.responses || d.outputs || {})[name] || "", style: "width:10em" });
    return el("label", { class: "field inline" }, el("span", { class: "mono" }, name), inputs[name]);
  });
  const dialog = modal(
    "Type this sheet's values",
    el("div", { class: "res-manual" }, el("p", { class: "muted small" }, "Saved values are marked as typed by hand, and the sheet counts as reviewed."), ...rows),
    [
      el("button", { class: "ghost", onclick: () => dialog.close() }, "Cancel"),
      el(
        "button",
        {
          class: "primary",
          onclick: async () => {
            const values = Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value]));
            try {
              await api(`/scans/${d.scan_id}/manual-values`, { method: "POST", json: { values } });
              dialog.close();
              toast("Values saved", "ok");
              await open(d.scan_id);
              load(r.offset, true);
            } catch (error) {
              toast(error.message, "error", 5000);
            }
          },
        },
        "Save"
      ),
    ],
    { wide: true }
  );
}

// Item 31: delete one sheet's result after a confirmation (audited)
function deleteScan() {
  if (!r.data || r.preview) return;
  const scanId = r.data.scan_id;
  const name = r.data.file_id || scanId.slice(0, 8);
  const go = el(
    "button",
    {
      class: "danger",
      onclick: async () => {
        go.disabled = true;
        try {
          await api(`/results/${scanId}`, { method: "DELETE" });
          dialog.close();
          toast(`Deleted ${name}`, "ok");
          emit("review-changed");
          const index = r.items.findIndex((i) => i.id === scanId);
          r.data = null;
          r.image = null;
          r.views = {};
          await load(r.offset, true);
          if (r.items.length) await openAt(Math.min(Math.max(index, 0), r.items.length - 1));
          refreshFacets();
        } catch (error) {
          go.disabled = false;
          toast(error.message, "error", 6000);
        }
      },
    },
    "Delete"
  );
  const dialog = modal(
    `Delete ${name}?`,
    el(
      "div",
      {},
      el("p", {}, "This deletes the sheet's result: its values, corrections, review items and the stored images. It cannot be undone."),
      el("p", { class: "small muted" }, r.data.source_path ? `The scan file itself (${r.data.source_path}) is not touched unless it was an upload.` : "The uploaded copy of the scan is deleted too."),
      el("p", { class: "small muted" }, "The deletion is written to the audit log with your name.")
    ),
    [el("button", { onclick: () => dialog.close() }, "Cancel"), go]
  );
  go.focus();
}

const REGRADE_EXAMPLE = '{"colorDropout": {"enabled": false}}';

// The main settings a sheet is usually re-read with; anything else goes in
// the JSON boxes under Advanced. "t": template override, "c": config override
const ON_OFF = [[true, "On"], [false, "Off"]];
const REGRADE_OPTIONS = [
  { label: "Turn the sheet", where: "t", path: ["alignment", "rotate"], choices: [[90, "90° clockwise"], [180, "Upside down (180°)"], [270, "90° anticlockwise"]], unset: "As scanned" },
  { label: "Colour removal", where: "t", path: ["colorDropout"], choices: [["grey", "Plain grey"], ["red", "Red channel (removes red print)"], ["green", "Green channel"], ["blue", "Blue channel"], ["max", "Brightest channel (removes any colour)"]] },
  { label: "Bubble threshold", where: "c", path: ["threshold_params", "mode"], choices: [["adaptive", "Adaptive (per sheet)"], ["fixed", "Fixed level"]] },
  { label: "Fixed level (0–255)", where: "c", path: ["threshold_params", "fixed_threshold"], number: { min: 1, max: 254, placeholder: "120" } },
  { label: "Mark sensitivity (lower counts fainter marks)", where: "c", path: ["threshold_params", "MIN_JUMP"], number: { min: 1, max: 255, placeholder: "25" } },
  { label: "Even out shadows", where: "c", path: ["threshold_params", "flatten_background"], choices: ON_OFF },
  { label: "Find the page outline (phone photos)", where: "t", path: ["alignment", "page_outline"], choices: ON_OFF },
  { label: "Fit blocks to their printed borders", where: "t", path: ["alignment", "rectify_on_border"], choices: ON_OFF },
  { label: "Per-block perspective", where: "t", path: ["alignment", "block_perspective"], choices: ON_OFF },
];

function pathGet(obj, path) {
  return path.reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
}

function pathSet(obj, path, value) {
  let o = obj;
  path.slice(0, -1).forEach((k) => {
    if (!o[k] || typeof o[k] !== "object") o[k] = {};
    o = o[k];
  });
  o[path[path.length - 1]] = value;
}

function pathDelete(obj, path) {
  const parents = [obj];
  for (const k of path.slice(0, -1)) {
    const next = parents[parents.length - 1][k];
    if (!next || typeof next !== "object") return;
    parents.push(next);
  }
  delete parents[parents.length - 1][path[path.length - 1]];
  // Drop the objects the removal left empty
  for (let i = parents.length - 1; i > 0; i--) {
    if (Object.keys(parents[i]).length) break;
    delete parents[i - 1][path[i - 1]];
  }
}

function deepMerge(base, extra) {
  const out = { ...base };
  Object.entries(extra || {}).forEach(([k, v]) => {
    out[k] = v && typeof v === "object" && !Array.isArray(v) && out[k] && typeof out[k] === "object" ? deepMerge(out[k], v) : v;
  });
  return out;
}

function showRegrade() {
  if (!r.data) return;
  const last = r.data.regrade || {};
  // Settings the controls show come out of the JSON boxes
  const rest = { t: JSON.parse(JSON.stringify(last.template_overrides || {})), c: JSON.parse(JSON.stringify(last.config_overrides || {})) };
  const controls = REGRADE_OPTIONS.map((option) => {
    const value = pathGet(rest[option.where], option.path);
    const known = option.number ? typeof value === "number" : option.choices.some(([v]) => v === value);
    if (value !== undefined && known) pathDelete(rest[option.where], option.path);
    const current = known ? value : undefined;
    let input;
    if (option.number) {
      input = el("input", { type: "number", min: option.number.min, max: option.number.max, placeholder: `template setting (${option.number.placeholder})`, value: current === undefined ? "" : String(current) });
    } else {
      input = el("select", {}, el("option", { value: "" }, option.unset || "Template setting"), option.choices.map(([v, text]) => el("option", { value: JSON.stringify(v) }, text)));
      input.value = current === undefined ? "" : JSON.stringify(current);
    }
    return { option, input };
  });
  const read = () => {
    const out = { t: {}, c: {} };
    controls.forEach(({ option, input }) => {
      const text = input.value.trim();
      if (text === "") return;
      pathSet(out[option.where], option.path, option.number ? Number(text) : JSON.parse(text));
    });
    return out;
  };
  const jsonText = (obj) => (Object.keys(obj).length ? JSON.stringify(obj, null, 1) : "");
  const templateBox = el("textarea", { rows: 5, placeholder: REGRADE_EXAMPLE }, jsonText(rest.t));
  const configBox = el("textarea", { rows: 3, placeholder: '{"review_params": {"min_confidence": 0.3}}' }, jsonText(rest.c));
  const keep = el("input", { type: "checkbox", checked: true });
  const current = el("input", { type: "checkbox" });
  const result = el("div", { class: "small" });
  const parse = (box) => {
    const text = box.value.trim();
    if (!text) return {};
    return JSON.parse(text);
  };
  const run = async (apply) => {
    let body;
    try {
      const chosen = read();
      // Advanced JSON wins over the controls where both set a value
      body = { template_overrides: deepMerge(chosen.t, parse(templateBox)), config_overrides: deepMerge(chosen.c, parse(configBox)), apply, keep_corrections: keep.checked, use_current_template: current.checked };
    } catch (e) {
      toast(`Invalid JSON: ${e.message}`, "error");
      return;
    }
    result.textContent = "Re-reading…";
    try {
      const data = await api(`/scans/${r.data.scan_id}/regrade`, { method: "POST", json: body });
      if (apply) {
        dialog.close();
        toast(`Regraded · ${data.changes.length} value(s) changed`, "ok");
        r.views = {};
        await open(r.data.scan_id, { keepView: true });
        return;
      }
      const image = await loadImage(fresh(data.image_url));
      r.preview = { data, image, body };
      result.innerHTML = "";
      result.append(changesTable(data.changes));
      renderSide();
      draw();
      updateStatus();
    } catch (error) {
      result.textContent = "";
      toast(error.message, "error", 6000);
    }
  };
  const advanced = el(
    "details",
    { class: "regrade-advanced" },
    el("summary", {}, "Advanced (JSON overrides)"),
    el("p", { class: "muted small" }, "Deep-merged into the template or the config; a value here wins over the controls above."),
    el("label", { class: "field" }, "Template overrides (JSON)", templateBox),
    el("label", { class: "field" }, "Config overrides (JSON)", configBox)
  );
  if (Object.keys(rest.t).length || Object.keys(rest.c).length) advanced.open = true;
  const dialog = modal(
    "Regrade this sheet",
    el(
      "div",
      {},
      el("p", { class: "muted small" }, "Re-reads the original file with these settings. Preview first; Apply replaces the stored read (the old one is kept in the history) and re-applies your corrections."),
      el("div", { class: "regrade-grid" }, controls.map(({ option, input }) => el("label", { class: "field" }, option.label, input))),
      advanced,
      el("label", { class: "small" }, keep, " keep my corrections"),
      " ",
      el("label", { class: "small" }, current, " use the current template version"),
      result
    ),
    [el("button", { onclick: () => run(false) }, "Preview"), el("button", { class: "primary", onclick: () => run(true) }, "Apply")],
    { wide: true }
  );
}

function changesTable(changes) {
  if (!changes.length) return el("p", { class: "muted" }, "No value or flag changes.");
  return el(
    "table",
    { class: "table" },
    el("thead", {}, el("tr", {}, el("th", {}, "Field"), el("th", {}, "Now"), el("th", {}, "Regraded"), el("th", {}, "Flags"))),
    el(
      "tbody",
      {},
      changes.map((c) => el("tr", {}, el("td", { class: "mono" }, c.name), el("td", { class: "mono" }, c.before === "" ? "∅" : c.before), el("td", { class: "mono" }, c.after === "" ? "∅" : c.after), el("td", {}, (c.flags_after || []).map((f) => chip(f, "flag")))))
    )
  );
}

function previewBox() {
  return el(
    "div",
    { class: "res-warning preview" },
    el("strong", {}, "Regrade preview — not saved. "),
    `${r.preview.data.changes.length} change(s). `,
    el(
      "button",
      {
        class: "small primary",
        onclick: async () => {
          const body = { ...r.preview.body, apply: true };
          try {
            await api(`/scans/${r.data.scan_id}/regrade`, { method: "POST", json: body });
            toast("Regrade applied", "ok");
            const backdrop = document.querySelector(".modal-backdrop");
            if (backdrop) backdrop.remove();
            r.views = {};
        await open(r.data.scan_id, { keepView: true });
          } catch (error) {
            toast(error.message, "error");
          }
        },
      },
      "Apply"
    ),
    " ",
    el(
      "button",
      {
        class: "small",
        onclick: () => {
          r.preview = null;
          renderSide();
          draw();
          updateStatus();
        },
      },
      "Discard"
    )
  );
}

async function showAccuracy() {
  const params = new URLSearchParams();
  if ($("res-template").value) params.set("template_id", $("res-template").value);
  if ($("res-job").value) params.set("job_id", $("res-job").value);
  let data;
  try {
    data = await api(`/results/accuracy?${params}`);
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  const pct = (v) => (v === null || v === undefined ? "–" : `${v.toFixed(2)}%`);
  modal(
    "Accuracy on verified sheets",
    el(
      "div",
      {},
      el("p", { class: "muted small" }, data.definition),
      el(
        "div",
        { class: "acc-tiles" },
        el("div", { class: "acc-tile" }, el("div", { class: "big" }, pct(data.auto_accuracy)), el("div", {}, "auto-accepted fields needing no correction"), el("div", { class: "muted small" }, `${data.auto_correct_fields} / ${data.auto_accepted_fields} fields`)),
        el("div", { class: "acc-tile" }, el("div", { class: "big" }, String(data.verified_sheets)), el("div", {}, "verified sheets")),
        el("div", { class: "acc-tile" }, el("div", { class: "big" }, pct(data.flag_precision)), el("div", {}, "of flagged fields needed a correction"), el("div", { class: "muted small" }, `${data.flagged_needed_correction} / ${data.flagged_fields}`))
      ),
      data.fields.length
        ? el(
            "table",
            { class: "table" },
            el("thead", {}, el("tr", {}, el("th", {}, "Field"), el("th", {}, "Auto-accepted"), el("th", {}, "Needed no correction"), el("th", {}, "Accuracy"), el("th", {}, "Flagged"), el("th", {}, "Flag precision"))),
            el(
              "tbody",
              {},
              data.fields.map((f) =>
                el(
                  "tr",
                  { class: f.auto_accuracy !== null && f.auto_accuracy < 100 ? "flagged" : "" },
                  el("td", { class: "mono" }, f.name),
                  el("td", {}, f.auto_accepted),
                  el("td", {}, f.auto_correct),
                  el("td", {}, pct(f.auto_accuracy)),
                  el("td", {}, f.flagged),
                  el("td", {}, pct(f.flag_precision))
                )
              )
            )
          )
        : el("p", { class: "muted" }, "Verify sheets on this screen (V) to measure accuracy.")
    ),
    [],
    { wide: true }
  );
}

async function showRemap() {
  let global;
  try {
    global = await api("/settings/path-remap");
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  const toText = (rules) => (rules || []).map((rule) => `${rule.from}=${rule.to}`).join("\n");
  const globalBox = el("textarea", { rows: 4, placeholder: "D:\\scans\\2025=E:\\archive\\2025" }, toText(global.rules));
  const jobId = $("res-job").value;
  let job = null;
  const jobBox = el("textarea", { rows: 3, placeholder: "old folder=new folder" });
  if (jobId) {
    try {
      job = await api(`/jobs/${jobId}`);
      jobBox.value = toText(job.path_remap);
    } catch (e) {
      job = null;
    }
  }
  const parse = (text) => text.split("\n").map((line) => line.trim()).filter(Boolean);
  const dialog = modal(
    "Path remap",
    el(
      "div",
      {},
      el("p", { class: "muted small" }, "When an input folder was moved, rewrite the start of the recorded paths (one old=new per line). The files themselves are never changed. Job rules are tried first, then these, then OMR_PATH_REMAP."),
      el("label", { class: "field" }, "All results", globalBox),
      global.environment && global.environment.length ? el("div", { class: "muted small" }, "From OMR_PATH_REMAP: ", el("code", {}, toText(global.environment))) : null,
      job ? el("label", { class: "field" }, `Job ${job.name || job.id.slice(0, 8)}`, jobBox) : el("p", { class: "muted small" }, "Pick a job in the filters to edit its own rules.")
    ),
    [
      el(
        "button",
        {
          class: "primary",
          onclick: async () => {
            try {
              await api("/settings/path-remap", { method: "PUT", json: { rules: parse(globalBox.value) } });
              if (job) await api(`/jobs/${job.id}`, { method: "PATCH", json: { path_remap: parse(jobBox.value) } });
              dialog.close();
              toast("Saved", "ok");
              r.views = {};
              if (r.data) open(r.data.scan_id, { keepView: true });
            } catch (error) {
              toast(error.message, "error", 6000);
            }
          },
        },
        "Save"
      ),
    ]
  );
}

export function userButtonLabel() {
  return state.user ? `User: ${state.user}` : "User";
}
