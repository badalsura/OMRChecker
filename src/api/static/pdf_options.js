// PDF rendering options on the Scan and New Job screens (config pdf_params,
// per batch). Empty = the template's config.json.
import { el } from "./api.js";
import { help } from "./editor_help.js";

const DPI = [["", "template default"], ["auto", "auto (the PDF's own resolution)"], ["150", "150"], ["200", "200"], ["300", "300"], ["400", "400"], ["600", "600"]];

export function pdfOptions(prefix) {
  const dpi = el("select", { id: `${prefix}-pdf-dpi` }, DPI.map(([v, t]) => el("option", { value: v }, t)));
  const page = el("input", { id: `${prefix}-pdf-page`, placeholder: "template default (1)", size: "12" });
  return el(
    "details",
    { class: "pdf-options" },
    el("summary", { class: "muted small" }, "PDF options"),
    help(el("label", { class: "field" }, "PDF render DPI", dpi), "PDF render DPI", "pdf"),
    help(el("label", { class: "field" }, "PDF pages", page), "PDF pages", "pdf")
  );
}

// Mount the options into a container element (by id)
export function mountPdfOptions(containerId, prefix) {
  const box = document.getElementById(containerId);
  if (box && !box.childElementCount) box.append(pdfOptions(prefix));
}

export function appendPdfParams(form, prefix) {
  const dpi = document.getElementById(`${prefix}-pdf-dpi`)?.value || "";
  const page = (document.getElementById(`${prefix}-pdf-page`)?.value || "").trim();
  if (dpi) form.append("pdf_dpi", dpi);
  if (page) form.append("pdf_page", page);
}
