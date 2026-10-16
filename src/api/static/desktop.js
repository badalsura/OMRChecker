// The app window (packaging/omr_app.py opens /?desktop=1): behave like an app,
// not a web page. No reload, back/forward, zoom or page menus; a file dropped
// beside a drop zone does not replace the screen; no API key (local only).
const EDITABLE = "input, textarea, select, [contenteditable=''], [contenteditable='true']";

export function isDesktop() {
  try {
    if (new URLSearchParams(location.search).has("desktop")) sessionStorage.setItem("omr_desktop", "1");
    return sessionStorage.getItem("omr_desktop") === "1";
  } catch (e) {
    return new URLSearchParams(location.search).has("desktop");
  }
}

function blockedKey(e) {
  const key = e.key;
  const ctrl = e.ctrlKey || e.metaKey;
  if (key === "F5" || key === "F12" || key === "BrowserBack" || key === "BrowserForward" || key === "BrowserRefresh") return true;
  if (e.altKey && (key === "ArrowLeft" || key === "ArrowRight" || key === "Home")) return true;
  if (ctrl && e.shiftKey && /^[ijcr]$/i.test(key)) return true;
  // reload, print page, save page, view source, find bar, new/close window, zoom
  if (ctrl && /^[rpsunwtgfj]$/i.test(key)) return true;
  if (ctrl && ["+", "-", "=", "0", "_"].includes(key)) return true;
  if (key === "Backspace" && !(e.target.closest && e.target.closest(EDITABLE))) return true;
  return false;
}

export function initDesktop() {
  document.documentElement.classList.add("desktop");
  document.body.classList.add("desktop");
  window.addEventListener(
    "keydown",
    (e) => {
      if (blockedKey(e)) e.preventDefault();
    },
    true
  );
  window.addEventListener(
    "wheel",
    (e) => {
      if (e.ctrlKey) e.preventDefault();
    },
    { passive: false, capture: true }
  );
  document.addEventListener("contextmenu", (e) => {
    if (!(e.target.closest && e.target.closest(EDITABLE))) e.preventDefault();
  });
  // Drop zones handle their own drops; anywhere else a dropped file would
  // open in the window instead of the app
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());
  // Middle-click or Ctrl+click would open a second window
  document.addEventListener(
    "click",
    (e) => {
      const link = e.target.closest && e.target.closest("a[href]");
      if (link && (e.ctrlKey || e.shiftKey || e.metaKey) && !link.hasAttribute("download")) e.preventDefault();
    },
    true
  );
  document.addEventListener("auxclick", (e) => {
    if (e.button === 1) e.preventDefault();
  });
}
