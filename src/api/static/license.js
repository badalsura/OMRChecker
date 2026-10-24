// Activation screen. Shown when the station has no valid license: the app's
// own endpoints are gated server-side (HTTP 402), so this screen is how a user
// activates. It shows this machine's request (to send the vendor) and takes
// the license file the vendor returns.
import { api, el, toast, url } from "./api.js";

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
  if (status.valid) return true;
  showActivation(status);
  return false;
}

async function showActivation(status) {
  document.getElementById("license-overlay")?.remove();
  let request = {};
  try {
    request = await api("/license/request");
  } catch (error) {
    /* shown below as unavailable */
  }
  const requestText = JSON.stringify(request, null, 2);
  fetch("/branding")
    .then((r) => (r.ok ? r.json() : null))
    .then((brand) => {
      if (brand && brand.name) for (const n of document.querySelectorAll("#license-overlay .brand-name")) n.textContent = brand.name;
    })
    .catch(() => {});

  const fileInput = el("input", { type: "file", accept: ".omrlic,.json,.txt", class: "lic-file" });
  const activateBtn = el("button", { class: "primary", onclick: () => activate(fileInput) }, "Activate");

  const overlay = el(
    "div",
    { id: "license-overlay", class: "lic-overlay" },
    el(
      "div",
      { class: "lic-card" },
      el("h1", {}, el("span", { class: "brand-name" }, "OMRChecker"), " — Activation"),
      el("p", { class: "lic-why" }, status.reason === "expired" ? "This license has expired." : status.message || "This machine is not licensed yet."),
      el("ol", { class: "lic-steps" },
        el("li", {}, "Send us the request below (it identifies this computer)."),
        el("li", {}, "We send back a license file."),
        el("li", {}, "Choose that file and click Activate.")
      ),
      el("label", { class: "lic-label" }, "1. Your request"),
      el("textarea", { class: "lic-request", readonly: true, rows: 7, onclick: (e) => e.target.select() }, requestText),
      el("div", { class: "row gap" },
        el("button", { class: "small", onclick: () => copy(requestText) }, "Copy"),
        el("a", { class: "button small", href: `data:application/json,${encodeURIComponent(requestText)}`, download: "omrchecker_request.json" }, "Save request file"),
        el("span", { class: "muted small" }, request.fingerprint ? `Machine: ${request.hostname || ""} · ${request.fingerprint}` : "")
      ),
      el("label", { class: "lic-label" }, "2. Your license file"),
      el("div", { class: "row gap wrap" }, fileInput, activateBtn),
      el("p", { class: "muted small" }, "The license file is checked on this computer; an edited or copied file will not work."),
      el("div", { class: "lic-foot muted small" }, "Already activated elsewhere? Make sure you are using the license issued for this computer.")
    )
  );
  document.body.append(overlay);
}

async function activate(fileInput) {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return toast("Choose the license file first", "", 3000);
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api("/license/activate", { method: "POST", form });
    toast(`Activated for ${result.license?.customer || "this machine"}.`, "ok", 3000);
    setTimeout(() => location.reload(), 800);
  } catch (error) {
    // The server sends a plain message (e.g. "...expired", "...different machine").
    toast(error.message.replace(/^\d+:\s*/, "") || "That license file was not accepted.", "error", 7000);
  }
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Request copied", "ok", 2000);
  } catch (error) {
    toast("Could not copy; select the text and copy it", "", 3000);
  }
}
