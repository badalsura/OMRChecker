// New Job screen: server folder picker (Browse), live path check, recent
// folders, "Include subfolders", help on both sources and the worker warning.
import { api, el, modal, state } from "./api.js";

const $ = (id) => document.getElementById(id);
let checkTimer = null;
let checkSeq = 0;

export function initFolderPicker() {
  const input = $("job-folder");
  if (!input) return;
  $("job-browse").addEventListener("click", () => openBrowser(input.value.trim()));
  input.addEventListener("input", scheduleCheck);
  input.addEventListener("change", scheduleCheck);
  $("job-recursive").addEventListener("change", scheduleCheck);
  $("job-recent").addEventListener("change", (e) => {
    if (!e.target.value) return;
    input.value = e.target.value;
    e.target.value = "";
    scheduleCheck();
  });
  $("job-workers").addEventListener("input", workersWarning);
  // Help line under each source while it is in use
  for (const box of document.querySelectorAll(".source-box[data-help]")) {
    box.addEventListener("focusin", () => box.classList.add("focused"));
    box.addEventListener("focusout", () => box.classList.remove("focused"));
  }
}

export function onShowFolders() {
  refreshRecent();
  workersWarning();
}

// "true"/"false" for the jobs form
export function includeSubfolders() {
  const box = $("job-recursive");
  return !box || box.checked;
}

export function workersWarning() {
  const out = $("job-workers-warning");
  if (!out) return;
  const workers = Number($("job-workers").value || 0);
  const cores = (state.caps && state.caps.cpu_count) || 0;
  out.textContent =
    cores && workers > cores
      ? `${workers} workers on ${cores} CPU cores: allowed, but more workers than cores only helps when reading from a slow disk or network share, and uses more memory.`
      : "";
}

async function refreshRecent() {
  const select = $("job-recent");
  if (!select) return;
  try {
    const data = await api("/fs/recent");
    select.innerHTML = "";
    select.append(el("option", { value: "" }, data.folders.length ? "Recent folders…" : "No recent folders"));
    for (const folder of data.folders) select.append(el("option", { value: folder }, folder));
    select.disabled = !data.folders.length;
  } catch (e) {
    select.disabled = true;
  }
}

function scheduleCheck() {
  clearTimeout(checkTimer);
  const out = $("job-folder-check");
  const path = $("job-folder").value.trim();
  if (!path) {
    out.textContent = "";
    out.className = "small muted";
    return;
  }
  out.textContent = "Checking…";
  out.className = "small muted";
  checkTimer = setTimeout(() => checkPath(path), 350);
}

export function countText(images, pdfs, truncated) {
  const n = (v) => Number(v || 0).toLocaleString();
  const parts = [`${n(images)} image${images === 1 ? "" : "s"}`];
  if (pdfs) parts.push(`${n(pdfs)} PDF${pdfs === 1 ? "" : "s"}`);
  return parts.join(", ") + (truncated ? " (or more)" : "");
}

async function checkPath(path) {
  const seq = ++checkSeq;
  const out = $("job-folder-check");
  const params = new URLSearchParams({ path, recursive: includeSubfolders() ? "true" : "false" });
  try {
    const data = await api(`/fs/check?${params}`);
    if (seq !== checkSeq) return;
    if (data.ok) {
      out.textContent = `✓ ${countText(data.images, data.pdfs, data.truncated)}`;
      out.className = "small fs-ok";
    } else {
      out.textContent = `✗ ${data.error}`;
      out.className = "small fs-bad";
    }
  } catch (error) {
    if (seq === checkSeq) {
      out.textContent = `✗ ${error.message}`;
      out.className = "small fs-bad";
    }
  }
}

// ---------------------------------------------------------------------------
// the Browse dialog
// ---------------------------------------------------------------------------
// onSelect(path) gets the chosen folder (default: the New Job folder box)
export async function openBrowser(start, onSelect) {
  const pathLine = el("div", { class: "fs-path mono" });
  const list = el("ul", { class: "fs-list" });
  const note = el("div", { class: "small muted" });
  const history = [];
  let current = null;
  let roots = null;

  const upButton = el("button", { class: "small", title: "Parent folder", onclick: () => current && current.parent && go(current.parent, true) }, "↑ Up");
  const backButton = el(
    "button",
    {
      class: "small",
      title: "Previous folder",
      onclick: () => {
        if (!history.length) return;
        const previous = history.pop();
        if (previous === null) showRoots(false);
        else go(previous, false);
      },
    },
    "← Back"
  );
  const drivesButton = el("button", { class: "small", onclick: () => showRoots(true) }, "Drives");
  const selectButton = el(
    "button",
    {
      class: "primary",
      disabled: true,
      onclick: () => {
        if (!current) return;
        dialog.close();
        if (onSelect) return onSelect(current.path);
        $("job-folder").value = current.path;
        scheduleCheck();
      },
    },
    "Select this folder"
  );

  const remember = () => history.push(current ? current.path : null);
  const updateButtons = () => {
    upButton.disabled = !current || !current.parent;
    backButton.disabled = !history.length;
    selectButton.disabled = !current;
  };

  async function showRoots(record) {
    if (record) remember();
    current = null;
    list.innerHTML = "";
    pathLine.textContent = "Drives and folders";
    try {
      roots = roots || (await api("/fs/roots"));
    } catch (error) {
      note.textContent = error.message;
      return;
    }
    if (roots.per_user && !roots.roots.length) note.textContent = "No server folders are allowed for your account. Upload files instead, or ask an administrator to allow a folder.";
    else if (roots.per_user) note.textContent = "Only the folders an administrator allowed for your account are shown.";
    else note.textContent = roots.restricted ? "Only the folders this server allows (OMR_ALLOWED_DIRS) are shown." : "";
    for (const root of roots.roots) {
      list.append(el("li", { onclick: () => go(root.path, true) }, el("span", { class: "fs-icon" }, root.kind === "drive" ? "🖴" : "📁"), el("span", { class: "fs-name" }, root.name)));
    }
    if ((roots.recent || []).length) {
      list.append(el("li", { class: "fs-sep muted small" }, "Recent folders"));
      for (const folder of roots.recent) list.append(el("li", { onclick: () => go(folder, true) }, el("span", { class: "fs-icon" }, "🕘"), el("span", { class: "fs-name" }, folder)));
    }
    updateButtons();
  }

  async function go(path, record) {
    let data;
    list.classList.add("loading");
    try {
      data = await api(`/fs/browse?path=${encodeURIComponent(path)}`);
    } catch (error) {
      note.textContent = error.message.replace(/^\d+: /, "");
      list.classList.remove("loading");
      return;
    } finally {
      list.classList.remove("loading");
    }
    if (record) remember();
    current = data;
    pathLine.textContent = data.path;
    note.textContent = `${countText(data.images, data.pdfs, data.truncated)} directly in this folder` + (data.more_folders ? ` · ${data.more_folders} more folders not listed` : "");
    list.innerHTML = "";
    if (!data.folders.length) list.append(el("li", { class: "muted small fs-sep" }, "No subfolders"));
    for (const folder of data.folders) {
      const count = folder.images || folder.pdfs ? countText(folder.images, folder.pdfs, folder.truncated) : "";
      list.append(
        el(
          "li",
          { onclick: () => go(folder.path, true), title: folder.path },
          el("span", { class: "fs-icon" }, "📁"),
          el("span", { class: "fs-name" }, folder.name),
          el("span", { class: "fs-count muted small" }, count)
        )
      );
    }
    updateButtons();
  }

  const dialog = modal(
    "Choose a folder on the server",
    el(
      "div",
      {},
      el("p", { class: "small muted" }, "These are the server's own folders. On the portable app the server is this PC. Counts show the images and PDFs directly in each folder."),
      el("div", { class: "row gap" }, drivesButton, backButton, upButton),
      pathLine,
      list,
      note
    ),
    [selectButton],
    { wide: true }
  );
  updateButtons();
  if (start) {
    await go(start, false);
    if (!current) await showRoots(false);
  } else {
    await showRoots(false);
  }
}
