// Templates tab: list, upload, auto-generate, and open the editor.
import { api, chip, el, errorList, fmtTime, loadTemplates, on, toast, url } from "./api.js";
import { TemplateEditor } from "./editor.js";
import { duplicateTemplate, renameTemplate } from "./template_ops.js";

let editor = null;

export function initTemplates() {
  document.getElementById("tpl-upload").addEventListener("click", upload);
  document.getElementById("gen-run").addEventListener("click", generate);
  document.getElementById("tpl-refresh").addEventListener("click", loadTemplates);
  on("templates", renderTable);
  editor = new TemplateEditor(document.getElementById("editor"), {
    onClose: () => {
      document.getElementById("editor").classList.add("hidden");
      document.getElementById("templates-home").classList.remove("hidden");
      loadTemplates();
    },
  });
  window.omrEditor = editor; // handy for debugging from the console
}

function renderTable(templates) {
  const body = document.querySelector("#tpl-table tbody");
  body.innerHTML = "";
  for (const t of templates) {
    body.append(
      el(
        "tr",
        {},
        el("td", {}, el("a", { href: "#", onclick: (e) => (e.preventDefault(), openEditor(t.id)) }, t.name)),
        el("td", { class: "mono muted" }, t.id),
        el("td", {}, chip(t.status, t.status)),
        el("td", {}, t.field_blocks),
        el("td", {}, t.zones),
        el("td", {}, t.has_evaluation ? "yes" : ""),
        el("td", { class: "muted small" }, fmtTime(t.updated_at)),
        el(
          "td",
          { class: "actions" },
          el("button", { class: "small primary", onclick: () => openEditor(t.id) }, "Edit"),
          el("a", { class: "button small", href: url(`/templates/${t.id}/layout.png`), target: "_blank" }, "Layout"),
          el("button", { class: "small", title: "Copy this layout (template, config, answer key, reference image) under a new name and open the copy", onclick: async () => { const copy = await duplicateTemplate(t); if (copy) openEditor(copy.id); } }, "Duplicate"),
          el("button", { class: "small", title: "Change the name; jobs, results and review items stay linked", onclick: () => renameTemplate(t) }, "Rename"),
          el(
            "button",
            {
              class: "small danger",
              onclick: async () => {
                if (!confirm(`Delete template '${t.name}'? Stored scans are kept.`)) return;
                try {
                  await api(`/templates/${t.id}`, { method: "DELETE" });
                  toast("Template deleted");
                  loadTemplates();
                } catch (error) {
                  toast(error.message, "error");
                }
              },
            },
            "Delete"
          )
        )
      )
    );
  }
  if (!templates.length) body.append(el("tr", {}, el("td", { colspan: 8, class: "muted" }, "No templates yet: upload one or generate one from samples.")));
}

export async function openEditor(templateId) {
  document.querySelector('.tabs button[data-tab="templates"]').click();
  document.getElementById("templates-home").classList.add("hidden");
  const root = document.getElementById("editor");
  root.classList.remove("hidden");
  try {
    await editor.open(templateId);
  } catch (error) {
    toast(error.message, "error");
    editor.onClose();
  }
}

async function upload() {
  const files = [...document.getElementById("tpl-files").files];
  const errorsBox = document.getElementById("tpl-upload-errors");
  errorsBox.innerHTML = "";
  if (!files.length) return toast("Choose template.json (plus assets) or a .zip", "error");
  const form = new FormData();
  const name = document.getElementById("tpl-name").value.trim();
  if (name) form.append("name", name);
  for (const f of files) form.append("files", f, f.name);
  try {
    const detail = await api("/templates", { method: "POST", form });
    toast(`Template '${detail.name}' uploaded`, "ok");
    document.getElementById("tpl-files").value = "";
    await loadTemplates();
  } catch (error) {
    errorsBox.append(el("div", { class: "chip error" }, error.message), errorList(error.errors));
  }
}

async function generate() {
  const files = [...document.getElementById("gen-files").files];
  if (!files.length) return toast("Choose sample sheets", "error");
  const status = document.getElementById("gen-status");
  const button = document.getElementById("gen-run");
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  const labels = document.getElementById("gen-labels").files[0];
  if (labels) form.append("labels", labels, labels.name);
  const name = document.getElementById("gen-name").value.trim();
  if (name) form.append("name", name);
  const options = document.getElementById("gen-options").value.trim();
  if (options) {
    try {
      JSON.parse(options);
    } catch (e) {
      return toast("Options must be valid JSON", "error");
    }
    form.append("options", options);
  }
  button.disabled = true;
  status.textContent = `Analysing ${files.length} sheets…`;
  try {
    const detail = await api("/templates/generate", { method: "POST", form });
    status.textContent = "";
    toast("Draft generated: verify the highlighted items, then save", "ok", 6000);
    await loadTemplates();
    openEditor(detail.id);
  } catch (error) {
    status.textContent = error.status === 501 ? "Template generation is not installed on this server." : error.message;
    toast(error.message, "error", 6000);
  } finally {
    button.disabled = false;
  }
}


