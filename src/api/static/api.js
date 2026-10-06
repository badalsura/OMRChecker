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
    select.append(el("option", { value: t.id }, `${t.name}${t.status === "draft" ? " (draft)" : ""}`));
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

export function download(filename, text, type = "text/csv") {
  const blob = new Blob([text], { type });
  const a = el("a", { href: URL.createObjectURL(blob), download: filename });
  document.body.append(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 1000);
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
