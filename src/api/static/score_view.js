// Per-section score summary shown in the Results window and the scoring preview.
import { el } from "./api.js";

const fmt = (v) => (v === null || v === undefined ? "" : String(Math.round(Number(v) * 100) / 100));

// scoring: {score, max_score, sections, section_max, counts, band}; null -> nothing
export function scoreSummary(scoring) {
  if (!scoring || typeof scoring !== "object" || scoring.score === undefined) return null;
  const sections = Object.entries(scoring.sections || {});
  const counts = scoring.counts || {};
  return el(
    "div",
    { class: "score-summary" },
    el(
      "div",
      { class: "row gap wrap" },
      el("strong", {}, `Score ${fmt(scoring.score)}`, scoring.max_score ? ` / ${fmt(scoring.max_score)}` : ""),
      scoring.band ? el("span", { class: "chip ok" }, scoring.band) : null,
      ["correct", "incorrect", "unmarked", "bonus", "dropped"]
        .filter((k) => counts[k])
        .map((k) => el("span", { class: `chip verdict-${k}`, title: k }, `${k} ${counts[k]}`))
    ),
    sections.length > 1 || (sections.length === 1 && sections[0][0] !== "DEFAULT")
      ? el(
          "table",
          { class: "table small score-sections" },
          el("thead", {}, el("tr", {}, el("th", {}, "Section"), el("th", {}, "Score"), el("th", {}, "Max"))),
          el(
            "tbody",
            {},
            sections.map(([name, value]) =>
              el("tr", {}, el("td", {}, name === "DEFAULT" ? "Other questions" : name), el("td", {}, fmt(value)), el("td", { class: "muted" }, fmt((scoring.section_max || {})[name])))
            )
          )
        )
      : null
  );
}
