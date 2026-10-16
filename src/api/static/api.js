// Shared helpers: API calls, DOM building, toasts and shared state.

export const state = {
  apiKey: safeStorage("get", "omr_api_key") || "",
  // Name recorded with corrections (sent as X-User); empty = "local"
  user: safeStorage("get", "omr_user") || "",
  caps: null,
  templates: [],
  listeners: {},
};

export function safeStorage(op, key, value) {
  try {
    if (op === "get") return localStorage.getItem(key);
    if (op === "set") localStorage.setItem(key, value);
  } catch (e) {
    /* storage unavailable */
  }
  return null;
}

export function setUser(name) {
  state.user = name || "";
  safeStorage("set", "omr_user", state.user);
}

export function setApiKey(key) {
  state.apiKey = key || "";
  safeStorage("set", "omr_api_key", state.apiKey);
}

export class ApiError extends Error {
  constructor(message, status, errors) {
    super(message);
    this.status = status;
    this.errors = errors || [];
  }
}

export async function api(path, { method = "GET", json, form, raw = false, retry = true } = {}) {
  const headers = {};
  if (state.apiKey) headers["X-API-Key"] = state.apiKey;
  if (state.user) headers["X-User"] = state.user;
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }
  const response = await fetch(path, { method, headers, body });
  if (response.status === 401 && retry && state.onUnauthorized && state.onUnauthorized()) {
    throw new ApiError("401: Sign in to continue", 401);
  }
  if (response.status === 401 && retry) {
    const key = prompt("This server requires an API key:", state.apiKey);
    if (key !== null) {
      setApiKey(key.trim());
      return api(path, { method, json, form, raw, retry: false });
    }
  }
  if (!response.ok) {
    let detail = response.statusText;
    let errors = [];
    try {
      const data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      errors = data.errors || [];
    } catch (e) {
      /* not JSON */
    }
    throw new ApiError(`${response.status}: ${detail}`, response.status, errors);
  }
  if (raw) return response;
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

// URL usable in <img src> / downloads (cannot send headers)
export function url(path) {
  if (!path) return path;
  if (!state.apiKey) return path;
  return path + (path.includes("?") ? "&" : "?") + "api_key=" + encodeURIComponent(state.apiKey);
}

// A name for display: never "null" / "undefined" (template menus, options, lists)
export function displayName(value, fallback = "") {
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim();
  return !text || text === "null" || text === "undefined" ? fallback : text;
}

// node.append() without the "null" text a browser writes for null children
export function add(node, ...children) {
  node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "html") node.innerHTML = value;
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function toast(message, kind = "", ms = 3500) {
  const node = el("div", { class: `toast ${kind}` }, message);
  document.getElementById("toasts").append(node);
  setTimeout(() => node.remove(), ms);
}

export function chip(text, kind) {
  return el("span", { class: `chip ${kind || text}` }, String(text).replace("_", " "));
}

export function errorList(errors) {
  if (!errors || !errors.length) return el("div");
  return el(
    "ul",
    { class: "error-list" },
    errors.map((e) => el("li", {}, el("code", {}, e.path || "$"), " ", e.message || String(e)))
  );
}

export function confidenceColor(conf) {
  if (conf === null || conf === undefined) return "transparent";
  const c = Math.max(0, Math.min(1, conf));
  // red (0) -> amber (0.5) -> green (1), low alpha so text stays readable
  const hue = Math.round(c * 120);
  return `hsla(${hue}, 75%, 45%, 0.22)`;
}

export function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

// --- tiny event bus between tabs ---
export function on(event, fn) {
  (state.listeners[event] = state.listeners[event] || []).push(fn);
}
export function emit(event, data) {
  for (const fn of state.listeners[event] || []) fn(data);
}

export async function loadTemplates() {
  const data = await api("/templates");
  state.templates = data.templates;
  for (const select of document.querySelectorAll("select.template-select")) {
    fillTemplateSelect(select);
  }
  emit("templates", state.templates);
  return state.templates;
}

export function fillTemplateSelect(select) {
  const current = select.value || safeStorage("get", "omr_tpl_" + select.id) || "";
  select.innerHTML = "";
  if (select.dataset.allowAll) select.append(el("option", { value: "" }, "All templates"));
  else if (!state.templates.length) select.append(el("option", { value: "" }, "No templates yet"));
  for (const t of state.templates) {
    select.append(el("option", { value: t.id }, `${displayName(t.name, t.id || "unnamed template")}${t.status === "draft" ? " (draft)" : ""}`));
  }
  if ([...select.options].some((o) => o.value === current)) select.value = current;
  if (!select.dataset.bound) {
    select.dataset.bound = "1";
    select.addEventListener("change", () => safeStorage("set", "omr_tpl_" + select.id, select.value));
  }
}

export function chunk(list, size) {
  const out = [];
  for (let i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
  return out;
}

// ---- Save As ---------------------------------------------------------------
// Every download asks where to save it (Chrome and Edge, and the app window):
// the Save As box opens first, then the file streams from the server straight
// into the chosen file, so even a large export never sits in memory. Browsers
// without a Save As box (Firefox, Safari) fall back to their own download.
const FILE_TYPES = {
  csv: ["CSV file", "text/csv"],
  xlsx: ["Excel workbook", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
  pdf: ["PDF document", "application/pdf"],
  sqlite: ["SQLite database", "application/vnd.sqlite3"],
  zip: ["ZIP archive", "application/zip"],
  json: ["JSON file", "application/json"],
  txt: ["Text file", "text/plain"],
};

async function pickTarget(name) {
  if (typeof window.showSaveFilePicker !== "function") return null;
  const ext = (name.split(".").pop() || "").toLowerCase();
  const known = FILE_TYPES[ext];
  const options = { suggestedName: name };
  if (known) options.types = [{ description: known[0], accept: { [known[1]]: [`.${ext}`] } }];
  try {
    return await window.showSaveFilePicker(options);
  } catch (error) {
    // Closing the box is a choice, not an error
    if (error && error.name === "AbortError") return false;
    return null;
  }
}

function clickDownload(href, name) {
  const a = el("a", { href, download: name || "" });
  document.body.append(a);
  a.click();
  setTimeout(() => a.remove(), 1000);
}

// Save a server file (path on this server) under a name the user picks.
// Returns true when saved, false when the Save As box was closed.
export async function saveAs(path, name) {
  const target = await pickTarget(name);
  if (target === false) return false;
  if (!target) {
    clickDownload(url(path), name);
    return true;
  }
  let writable;
  try {
    const response = await api(path, { raw: true });
    writable = await target.createWritable();
    await response.body.pipeTo(writable);
    toast(`Saved ${target.name}`, "ok");
    return true;
  } catch (error) {
    if (writable) {
      try {
        await writable.abort();
      } catch (e) {
        /* already closed */
      }
    }
    toast(`Could not save ${target.name}: ${error.message}`, "error", 6000);
    return false;
  }
}

// A name safe to suggest for a file: letters, digits, spaces, - _ . ( )
export function fileStem(name, fallback = "file") {
  const stem = String(name || "").replace(/[^\w\s.()-]+/g, "-").replace(/\s+/g, " ").trim().slice(0, 80);
  return stem || fallback;
}

// A link-like button that saves a server file with Save As
export function saveButton(path, name, label, attrs = {}) {
  return el("button", { ...attrs, onclick: () => saveAs(path, name) }, label);
}

// Save text made in the page (a CSV of scan results, a JSON file)
export async function download(filename, text, type = "text/csv") {
  const blob = new Blob([text], { type });
  const target = await pickTarget(filename);
  if (target === false) return false;
  if (target) {
    try {
      const writable = await target.createWritable();
      await writable.write(blob);
      await writable.close();
      toast(`Saved ${target.name}`, "ok");
      return true;
    } catch (error) {
      toast(`Could not save ${target.name}: ${error.message}`, "error", 6000);
      return false;
    }
  }
  const href = URL.createObjectURL(blob);
  clickDownload(href, filename);
  setTimeout(() => URL.revokeObjectURL(href), 1000);
  return true;
}

export function csvCell(value) {
  const s = value === null || value === undefined ? "" : String(value);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// Modal dialog: returns {close, box}; Esc or a click outside closes it.
export function modal(title, body, actions = [], { wide = false, onClose } = {}) {
  const box = el("div", { class: `modal${wide ? " wide" : ""}`, role: "dialog" });
  const backdrop = el("div", { class: "modal-backdrop" }, box);
  const close = () => {
    backdrop.remove();
    document.removeEventListener("keydown", onKey, true);
    if (onClose) onClose();
  };
  const onKey = (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      close();
    }
  };
  box.append(
    el("div", { class: "modal-head" }, el("h2", {}, title), el("button", { class: "ghost small", onclick: close, title: "Close (Esc)" }, "✕")),
    el("div", { class: "modal-body" }, body),
    el("div", { class: "modal-actions" }, actions)
  );
  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop) close();
  });
  document.addEventListener("keydown", onKey, true);
  document.body.append(backdrop);
  return { close, box };
}
