// Activation screen. Shown when the station has no valid license: the app's
// own endpoints are gated server-side (HTTP 402), so this screen is how a user
// activates. It shows what is wrong and what to do, this computer's request
// code (to send the vendor) and takes the license file the vendor returns.
import { api, download, el, modal, state, toast } from "./api.js";

// Days before expiry from which the License button and panel warn
const WARN_DAYS = 14;

// Returns true when the station is licensed; otherwise shows the activation
// screen and returns false (the caller then stops booting the app).
export async function ensureLicensed() {
  let status;
  try {
    status = await api("/license/status");
  } catch (error) {
    // If the status call itself fails, let the app try to boot rather than
    // locking the user out over a transient error.
    return true;
  }
  // A working call refused later (license expired while the app was open)
  // brings the activation screen back instead of a raw error
  state.onUnlicensed = (detail) => {
    if (!document.getElementById("license-overlay")) showActivation({ valid: false, ...detail });
  };
  if (status.valid) {
    markLicenseButton(status);
    return true;
  }
  showActivation(status);
  return false;
}

const day = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "");
const daysLeft = (iso) => (iso ? Math.ceil((new Date(iso) - Date.now()) / 86400000) : null);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// The topbar button says when the license runs out soon
function markLicenseButton(status) {
  const button = document.getElementById("license-btn");
  const left = daysLeft(status.license?.expires_at);
  if (!button || left === null || left > WARN_DAYS) return;
  button.textContent = left <= 0 ? "License expires today" : `License · ${plural(left, "day")} left`;
  button.classList.add("lic-soon");
  button.title = `This license expires on ${day(status.license.expires_at)}. Open to renew it.`;
}

async function loadRequest() {
  try {
    return await api("/license/request");
  } catch (error) {
    return {};
  }
}

// Request code with Copy and Save request file
function requestBlock(request) {
  if (!request.code) return el("p", { class: "lic-bad" }, "This computer's request code could not be read. Restart the app and try again.");
  return el(
    "div",
    { class: "lic-request-box" },
    el("div", { class: "lic-code", title: "Click to select" , onclick: (e) => selectText(e.currentTarget) }, request.code),
    el(
      "div",
      { class: "row gap wrap" },
      el("button", { class: "small primary", onclick: () => copy(request.code, "Request code copied") }, "Copy request code"),
      el("button", { class: "small", onclick: () => download("omrchecker-license-request.txt", request.request_file, "text/plain") }, "Save request file"),
      el("span", { class: "muted small" }, request.hostname ? `This computer: ${request.hostname}` : "")
    )
  );
}

function licenseLine(lic) {
  if (!lic || !lic.customer) return null;
  const parts = [`Licensed to ${lic.customer}`];
  if (lic.expires_at) parts.push(`${daysLeft(lic.expires_at) < 0 ? "expired" : "expires"} ${day(lic.expires_at)}`);
  return el("p", { class: "muted small" }, parts.join(" · "));
}

async function showActivation(status) {
  document.getElementById("license-overlay")?.remove();
  const request = await loadRequest();
  fetch("/branding")
    .then((r) => (r.ok ? r.json() : null))
    .then((brand) => {
      if (brand && brand.name) for (const n of document.querySelectorAll("#license-overlay .brand-name")) n.textContent = brand.name;
    })
    .catch(() => {});

  const fileInput = el("input", { type: "file", accept: ".omrlic,.json,.txt", class: "lic-file" });
  const activateBtn = el("button", { class: "primary", onclick: () => activate(fileInput) }, "Activate");
  const firstTime = !status.reason || status.reason === "no_license";
  const steps = firstTime
    ? ["Send us the request code below. It identifies this computer.", "We send back a license file.", "Choose that file and click Activate."]
    : ["Send us the request code below.", "We send back a new license file.", "Choose that file and click Activate."];

  const overlay = el(
    "div",
    { id: "license-overlay", class: "lic-overlay" },
    el(
      "div",
      { class: "lic-card" },
      el("h1", {}, el("span", { class: "brand-name" }, "OMRChecker"), firstTime ? " — Activation" : " — License"),
      el("p", { class: firstTime ? "lic-why lic-first" : "lic-why" }, status.message || "This computer is not licensed yet."),
      status.action && !firstTime ? el("p", { class: "lic-action" }, status.action) : null,
      licenseLine(status.license),
      el("ol", { class: "lic-steps" }, ...steps.map((s) => el("li", {}, s))),
      el("label", { class: "lic-label" }, "1. Your request code"),
      requestBlock(request),
      el("label", { class: "lic-label" }, "2. Your license file"),
      el("div", { class: "row gap wrap" }, fileInput, activateBtn),
      el("p", { class: "muted small" }, "The license file is checked on this computer. An edited file, or one issued for another computer, will not work.")
    )
  );
  document.body.append(overlay);
}

// The License panel (topbar): who this copy is licensed to, until when, for
// which features and machine, and a way to load a replacement license file.
export async function openLicensePanel() {
  let status = {};
  try {
    status = await api("/license/status");
  } catch (error) {
    return toast(`Could not read the license: ${error.message}`, "error", 5000);
  }
  const request = await loadRequest();
  const lic = status.license || {};
  const left = daysLeft(lic.expires_at);
  const expires = lic.expires_at ? `${day(lic.expires_at)}${left >= 0 ? ` (${plural(left, "day")} left)` : ""}` : "Never";
  const rows = [
    ["Status", status.valid ? "Licensed" : status.message],
    ["Licensed to", lic.customer],
    ["Edition", lic.edition],
    ["Features", (lic.features || []).join(", ")],
    ["Seats", lic.seats],
    ["Issued", day(lic.issued_at)],
    ["Expires", status.license ? expires : ""],
    ["Bound to", status.license ? (lic.machine_bound ? "This computer" : "Any computer (floating)") : ""],
    ["License id", lic.license_id],
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");
  const soon = status.valid && left !== null && left <= WARN_DAYS;
  const fileInput = el("input", { type: "file", accept: ".omrlic,.json,.txt", class: "lic-file" });
  const body = el(
    "div",
    { class: "lic-panel" },
    el("dl", { class: "lic-facts" }, ...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", { class: k === "Status" && !status.valid ? "lic-bad" : "" }, String(v))])),
    !status.valid && status.action ? el("p", { class: "lic-action" }, status.action) : null,
    soon ? el("p", { class: "lic-action" }, `This license runs out ${left <= 0 ? "today" : `in ${plural(left, "day")}`}. To renew, send the request code below to your vendor.`) : null,
    el("label", { class: "lic-label" }, "Request code for a new license"),
    requestBlock(request),
    el("label", { class: "lic-label" }, "Replace the license"),
    el("div", { class: "row gap wrap" }, fileInput, el("button", { class: "small", onclick: () => activate(fileInput) }, "Load license"))
  );
  modal("License", body, []);
}

async function activate(fileInput) {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return toast("Choose the license file first", "", 3000);
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api("/license/activate", { method: "POST", form });
    toast(`Activated for ${result.license?.customer || "this computer"}.`, "ok", 3000);
    setTimeout(() => location.reload(), 800);
  } catch (error) {
    // The server sends what is wrong and what to do, as plain text
    toast(error.message.replace(/^\d+:\s*/, "") || "That license file was not accepted.", "error", 9000);
  }
}

function selectText(node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

async function copy(text, done) {
  try {
    await navigator.clipboard.writeText(text);
    toast(done, "ok", 2000);
  } catch (error) {
    toast("Could not copy: select the code and copy it", "", 3000);
  }
}
