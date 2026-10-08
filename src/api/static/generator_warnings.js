// Generator warnings in the editor's report box (Gemini 24/35): each warning
// gets a "Confirm" button that clears it (kept on the server as confirmed, so
// it can be shown again), plus "Confirm all". Information notes (e.g. "scans
// are cropped to the paper; whole image used") are listed separately: they
// need no action.
import { api, el, toast } from "./api.js";

async function acknowledge(ed, body) {
  try {
    const data = await api(`/templates/${ed.id}/generator/acknowledge`, { method: "POST", json: body });
    ed.detail.report = { ...(ed.detail.report || {}), ...data };
    ed.report = ed.parseReport(ed.detail.report);
    ed.renderSide();
  } catch (error) {
    toast(error.message, "error");
  }
}

export function renderWarnings(ed, warnings) {
  const report = (ed.detail && ed.detail.report) || {};
  const info = Array.isArray(report.info) ? report.info : [];
  const confirmed = Array.isArray(report.acknowledged_warnings) ? report.acknowledged_warnings : [];
  const box = el("div", {});
  if (warnings.length) {
    box.append(
      el(
        "div",
        { class: "row gap" },
        el("h3", {}, `Warnings (${warnings.length})`),
        el("span", { class: "spacer" }),
        el("button", { class: "small", title: "Mark every warning as checked; they are cleared from this list", onclick: () => acknowledge(ed, { all: true }) }, "Confirm all")
      ),
      el(
        "ul",
        {},
        warnings.map((w) =>
          el(
            "li",
            {},
            w,
            " ",
            el("button", { class: "small ghost", title: "I have checked this: clear it", onclick: () => acknowledge(ed, { warnings: [w] }) }, "Confirm")
          )
        )
      )
    );
  }
  if (info.length) {
    box.append(el("h3", {}, "Information"), el("ul", { class: "muted small" }, info.map((w) => el("li", {}, w))));
  }
  if (confirmed.length) {
    box.append(
      el(
        "details",
        { class: "muted small" },
        el("summary", {}, `${confirmed.length} confirmed warning(s)`),
        el("ul", {}, confirmed.map((w) => el("li", {}, w))),
        el("button", { class: "small ghost", onclick: () => acknowledge(ed, { undo: true }) }, "Show them again")
      )
    );
  }
  return box;
}
