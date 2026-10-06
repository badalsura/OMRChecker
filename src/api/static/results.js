// Results tab: browse every graded sheet, see the exact overlay it was graded
// with (re-rendered by the server, drawn here), correct it by clicking bubbles
// or typing, verify it, regrade it with overrides, and read the accuracy.
import { api, chip, confidenceColor, el, emit, modal, on, safeStorage, state, toast, url } from "./api.js";

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
  hover: null,
  busy: false,
  autoAdvance: safeStorage("get", "omr_res_advance") !== "0",
  prefetched: new Set(),
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
        item.flag_count ? el("span", { class: "chip flag", title: "fields flagged when read" }, `⚑${item.flag_count}`) : null,
        item.corrected ? el("span", { class: "chip corrected", title: "corrected" }, "✎") : null,
        item.verified ? el("span", { class: "chip ok", title: "verified" }, "✓") : null,
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

async function open(scanId, { keepView = false } = {}) {
  r.busy = true;
  statusLine.textContent = "Rendering…";
  r.preview = null;
  try {
    const data = await api(`/scans/${scanId}/render`);
    const image = await loadImage(fresh(data.image_url));
    const sameSize = r.image && r.image.naturalWidth === image.naturalWidth && r.image.naturalHeight === image.naturalHeight;
    r.data = data;
    r.image = image;
    if (!r.selected || !findItem(r.selected)) r.selected = null;
    if (!keepView && !sameSize) fit();
    renderList();
    renderSide();
    draw();
    prefetch();
  } catch (error) {
    // Show the values even when no image can be produced
    try {
      r.data = await api(`/scans/${scanId}/overlay`);
      r.data.warnings = [error.message];
      r.image = null;
      renderList();
      renderSide();
      draw();
    } catch (e) {
      toast(error.message, "error", 6000);
    }
  } finally {
    r.busy = false;
    updateStatus();
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
  const keep = { image_url: r.data.image_url, width: r.data.width, height: r.data.height, image_source: r.data.image_source, warnings: r.data.warnings, drift: r.data.drift, resolved_path: r.data.resolved_path };
  r.data = Object.assign(data, keep);
  const item = r.items.find((i) => i.id === data.scan_id);
  if (item) {
    item.verified = data.verified ? 1 : 0;
    item.corrected = data.corrected ? 1 : 0;
    item.status = data.status;
  }
  renderList();
  renderSide();
  draw();
}

async function correct(body) {
  if (!r.data || r.preview) return;
  try {
    const data = await api(`/scans/${r.data.scan_id}/corrections`, { method: "POST", json: body });
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

function fit() {
  const wrap = canvas.parentElement;
  const [w, h] = imageSize();
  const scale = Math.min((wrap.clientWidth - 20) / w, (wrap.clientHeight - 20) / h);
  r.view.scale = scale > 0 ? scale : 1;
  r.view.x = (wrap.clientWidth - w * r.view.scale) / 2;
  r.view.y = (wrap.clientHeight - h * r.view.scale) / 2;
}

function zoom(factor, cx, cy) {
  const wrap = canvas.parentElement;
  if (cx === undefined) {
    cx = wrap.clientWidth / 2;
    cy = wrap.clientHeight / 2;
  }
  const scale = Math.max(0.05, Math.min(12, r.view.scale * factor));
  const k = scale / r.view.scale;
  r.view.x = cx - (cx - r.view.x) * k;
  r.view.y = cy - (cy - r.view.y) * k;
  r.view.scale = scale;
  draw();
}

function toImage(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  return { x: (clientX - rect.left - r.view.x) / r.view.scale, y: (clientY - rect.top - r.view.y) / r.view.scale };
}

function hitTest(p) {
  if (!r.data || !r.overlay) return null;
  const d = r.preview ? r.preview.data : r.data;
  const pad = 2;
  for (const field of d.fields) {
    const box = field.box;
    if (!box || p.x < box[0] - pad || p.y < box[1] - pad || p.x > box[0] + box[2] + pad || p.y > box[1] + box[3] + pad) continue;
    for (const b of field.bubbles) {
      if (p.x >= b.x - pad && p.x <= b.x + b.w + pad && p.y >= b.y - pad && p.y <= b.y + b.h + pad) return { kind: "bubble", field, bubble: b };
    }
    return { kind: "field", field };
  }
  for (const zone of d.zones) {
    const box = zone.box;
    if (box && box.length === 4 && p.x >= box[0] && p.y >= box[1] && p.x <= box[0] + box[2] && p.y <= box[1] + box[3]) return { kind: "zone", zone };
  }
  return null;
}

function bindCanvas() {
  let drag = null;
  canvas.addEventListener("mousedown", (e) => {
    drag = { x: e.clientX, y: e.clientY, vx: r.view.x, vy: r.view.y, moved: false };
  });
  window.addEventListener("mousemove", (e) => {
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
    canvas.style.cursor = hit ? "pointer" : "grab";
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
    if (!was || was.moved || e.target !== canvas) return;
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
      zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - rect.left, e.clientY - rect.top);
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
  corrected: "#8e44ad",
  selected: "#d0342c",
  zone: { barcode: "#c85a14", qrcode: "#a028a0", ocr: "#1e8c1e", icr: "#1478dc" },
};

function draw() {
  if (!ctx2d) return;
  const dpr = window.devicePixelRatio || 1;
  ctx2d.setTransform(1, 0, 0, 1, 0, 0);
  ctx2d.clearRect(0, 0, canvas.width, canvas.height);
  ctx2d.setTransform(dpr * r.view.scale, 0, 0, dpr * r.view.scale, dpr * r.view.x, dpr * r.view.y);
  const img = r.preview ? r.preview.image : r.image;
  const [w, h] = imageSize();
  ctx2d.fillStyle = "#fff";
  ctx2d.fillRect(0, 0, w, h);
  if (img) {
    ctx2d.imageSmoothingEnabled = r.view.scale < 2;
    ctx2d.drawImage(img, 0, 0);
  }
  if (!r.data || !r.overlay) return;
  const d = r.preview ? r.preview.data : r.data;
  const px = 1 / r.view.scale; // one screen pixel in image units
  for (const field of d.fields) drawField(field, px);
  for (const zone of d.zones) drawZone(zone, px);
}

function drawField(field, px) {
  const selected = field.name === r.selected;
  for (const b of field.bubbles) {
    const hovered = r.hover === `${field.name}/${b.value}`;
    if (b.marked) {
      ctx2d.fillStyle = COLORS.marked;
      ctx2d.fillRect(b.x, b.y, b.w, b.h);
    }
    ctx2d.lineWidth = (b.marked ? 2 : 1) * px * (hovered ? 2 : 1);
    ctx2d.strokeStyle = b.marked ? COLORS.markedLine : COLORS.empty;
    if (field.flagged) ctx2d.strokeStyle = b.marked ? COLORS.markedLine : COLORS.flagged;
    ctx2d.strokeRect(b.x, b.y, b.w, b.h);
    if (b.marked !== b.read_marked) {
      // A person changed this bubble: mark the difference to the machine read
      ctx2d.strokeStyle = COLORS.corrected;
      ctx2d.lineWidth = 2.5 * px;
      ctx2d.beginPath();
      ctx2d.arc(b.cx, b.cy, Math.max(b.w, b.h) * 0.62, 0, Math.PI * 2);
      ctx2d.stroke();
    }
  }
  if (!field.box) return;
  const [x, y, w, h] = field.box;
  if (field.flagged || selected || field.corrected) {
    ctx2d.lineWidth = (selected ? 3 : 2) * px;
    ctx2d.strokeStyle = selected ? COLORS.selected : field.corrected ? COLORS.corrected : COLORS.flagged;
    if (field.flagged && !selected && field.pending) ctx2d.setLineDash([6 * px, 4 * px]);
    ctx2d.strokeRect(x - 4 * px, y - 4 * px, w + 8 * px, h + 8 * px);
    ctx2d.setLineDash([]);
  }
  if (selected || field.flagged || r.view.scale > 0.9) label(`${field.name}: ${field.value === "" ? "∅" : field.value}`, x, y - 6 * px, px, selected ? COLORS.selected : field.flagged ? COLORS.flagged : "#333");
}

function drawZone(zone, px) {
  if (!zone.box || zone.box.length !== 4) return;
  const [x, y, w, h] = zone.box;
  const selected = zone.name === r.selected;
  ctx2d.lineWidth = (selected ? 3 : 2) * px;
  ctx2d.strokeStyle = selected ? COLORS.selected : zone.flagged ? COLORS.flagged : zone.corrected ? COLORS.corrected : COLORS.zone[zone.type] || "#333";
  ctx2d.strokeRect(x, y, w, h);
  label(`${zone.name}: ${zone.value === "" ? "∅" : zone.value}`, x, y - 6 * px, px, ctx2d.strokeStyle);
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
  }
  if (r.preview) parts.push("REGRADE PREVIEW (not saved)");
  statusLine.textContent = parts.join("  ·  ");
}

function centerOn(item) {
  const box = item.box;
  if (!box) return;
  const wrap = canvas.parentElement;
  const sx = r.view.x + box[0] * r.view.scale;
  const sy = r.view.y + box[1] * r.view.scale;
  const ex = sx + box[2] * r.view.scale;
  const ey = sy + box[3] * r.view.scale;
  if (sx >= 0 && sy >= 0 && ex <= wrap.clientWidth && ey <= wrap.clientHeight) return;
  r.view.x = wrap.clientWidth / 2 - (box[0] + box[2] / 2) * r.view.scale;
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
  if (!item.pending || r.preview) return null;
  return el(
    "button",
    {
      class: "small ghost res-accept",
      title: "The value is right: settle this item",
      onclick: (e) => {
        e.preventDefault();
        e.stopPropagation();
        correct({ accept: [item.check || item.name] });
      },
    },
    "Accept"
  );
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
      item.corrected ? el("span", { class: "chip corrected" }, `was ${item.original_value === "" || item.original_value === undefined ? "∅" : item.original_value}`) : null,
      (item.flags || []).map((f) => chip(f, "flag")),
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
    el("div", { class: "muted small res-path" }, d.resolved_path || d.source_path || "")
  );
  for (const warning of r.data.warnings || []) side.append(el("div", { class: "res-warning" }, warning));
  if (r.data.drift && r.data.drift.length) side.append(el("div", { class: "res-warning" }, `Re-reading now gives different values for: ${r.data.drift.join(", ")}`));
  if (d.error) side.append(el("div", { class: "res-warning error" }, d.error));
  if (r.preview) side.append(previewBox());

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
      const head = el("summary", { class: `res-row group${output.flagged ? " flagged" : ""}${output.corrected ? " corrected" : ""}`, "data-names": [output.name, ...output.parts].join("\n") }, el("span", { class: "res-name" }, output.name), joined, el("span", { class: "conf", style: { background: confidenceColor(output.confidence) } }, confText(output.confidence)), el("div", { class: "res-flags" }, acceptButton(output), (output.flags || []).map((f) => chip(f, "flag")), (output.reasons || []).length ? el("span", { class: "muted small res-reasons" }, output.reasons.join("; ")) : null));
      const details = el("details", { class: "res-group" }, head, parts.map((p) => itemRow(p, true)));
      if (output.flagged || output.corrected) details.open = true;
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
  } else if (k === "n" || k === "Tab") cycleFlagged(e.shiftKey ? -1 : 1);
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
}

const REGRADE_EXAMPLE = '{"colorDropout": {"enabled": false}}';

function showRegrade() {
  if (!r.data) return;
  const last = r.data.regrade || {};
  const templateBox = el("textarea", { rows: 5, placeholder: REGRADE_EXAMPLE }, Object.keys(last.template_overrides || {}).length ? JSON.stringify(last.template_overrides, null, 1) : "");
  const configBox = el("textarea", { rows: 3, placeholder: '{"threshold_params": {"MIN_JUMP": 30}}' }, Object.keys(last.config_overrides || {}).length ? JSON.stringify(last.config_overrides, null, 1) : "");
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
      body = { template_overrides: parse(templateBox), config_overrides: parse(configBox), apply, keep_corrections: keep.checked, use_current_template: current.checked };
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
  const dialog = modal(
    "Regrade this sheet",
    el(
      "div",
      {},
      el("p", { class: "muted small" }, "Re-reads the original file with overrides deep-merged into the template (e.g. colour removal) or the config. Preview first; Apply replaces the stored read (the old one is kept in the history) and re-applies your corrections."),
      el("label", { class: "field" }, "Template overrides (JSON)", templateBox),
      el("label", { class: "field" }, "Config overrides (JSON)", configBox),
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
