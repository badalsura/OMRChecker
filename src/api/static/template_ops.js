// Duplicate and rename a layout (plan item 7). Used by the Templates tab and the editor title.
import { api, el, loadTemplates, modal, state, toast } from "./api.js";

// Asks for a name in a small dialog; resolves to the trimmed name or null.
// check(name) may return an error message shown under the box (e.g. name in use).
export function askName(title, initial, help, check) {
  return new Promise((resolve) => {
    const input = el("input", { type: "text", value: initial, maxlength: 120, style: { width: "100%" } });
    const error = el("div", { class: "chip error hidden" });
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      dialog.close();
      resolve(value);
    };
    const ok = () => {
      const name = input.value.trim().replace(/\s+/g, " ");
      const problem = !name ? "The name can't be empty" : check ? check(name) : null;
      if (problem) {
        error.textContent = problem;
        error.classList.remove("hidden");
        input.focus();
        return;
      }
      finish(name);
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") ok();
    });
    const dialog = modal(
      title,
      el("div", {}, el("label", { class: "field" }, "Name", input), el("p", { class: "muted small" }, help), error),
      [el("button", { class: "ghost", onclick: () => finish(null) }, "Cancel"), el("button", { class: "primary", onclick: ok }, "OK")],
      { onClose: () => finish(null) }
    );
    setTimeout(() => {
      input.focus();
      input.select();
    }, 0);
  });
}

// Names are unique, ignoring case; the server checks again.
export function nameInUse(name, exceptId) {
  const key = name.toLocaleLowerCase();
  const other = (state.templates || []).find((t) => t.id !== exceptId && String(t.name || "").toLocaleLowerCase() === key);
  return other ? `The name '${name}' is already used by another layout` : null;
}

function uniqueCopyName(name) {
  let candidate = `${name} copy`;
  for (let i = 2; nameInUse(candidate) && i < 1000; i++) candidate = `${name} copy ${i}`;
  return candidate;
}

// Returns the new template detail, or null when cancelled or refused.
export async function duplicateTemplate(template) {
  const name = await askName(
    `Duplicate '${template.name}'`,
    uniqueCopyName(template.name),
    "Copies the layout, its config, answer key and reference image. Scans and results of the original stay with the original.",
    (n) => nameInUse(n)
  );
  if (!name) return null;
  try {
    const detail = await api(`/templates/${encodeURIComponent(template.id)}/duplicate`, { method: "POST", json: { name } });
    toast(`Copied as '${detail.name}'`, "ok");
    await loadTemplates();
    return detail;
  } catch (error) {
    toast(error.message, "error", 6000);
    return null;
  }
}

// Returns the new name, or null when cancelled or refused.
export async function renameTemplate(template) {
  const name = await askName(
    `Rename '${template.name}'`,
    template.name,
    "Only the name changes: past jobs, results and review items stay linked to this layout.",
    (n) => nameInUse(n, template.id)
  );
  if (!name || name === template.name) return null;
  try {
    const detail = await api(`/templates/${encodeURIComponent(template.id)}/rename`, { method: "POST", json: { name } });
    toast(`Renamed to '${detail.name}'`, "ok");
    await loadTemplates();
    return detail.name;
  } catch (error) {
    toast(error.message, "error", 6000);
    return null;
  }
}
