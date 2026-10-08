// Scoring panel of the template editor (plan item 12, with tracker items 21 and 39):
// answer key grid, paste / CSV / Excel / master-sheet import, several correct answers,
// weighted options, marking presets with fractions, sections, bonus and dropped
// questions, multi-marked rule, grade switch, question ranges, score legend and a
// preview on a sample sheet. Writes evaluation.json; Apply is one undo step.
import { api, el, toast } from "./api.js";
import { scoreSummary } from "./score_view.js";

export const PRESETS = [
  { label: "+4 / −1 / 0", correct: "4", incorrect: "-1", unmarked: "0" },
  { label: "+1 / −1/4 / 0", correct: "1", incorrect: "-1/4", unmarked: "0" },
  { label: "+1 / −1/3 / 0", correct: "1", incorrect: "-1/3", unmarked: "0" },
  { label: "+1 / 0 / 0", correct: "1", incorrect: "0", unmarked: "0" },
];
const MULTI_RULES = [
  ["default", "Use the answer key (weights, else wrong)"],
  ["incorrect", "Score as wrong"],
  ["unmarked", "Score as unmarked"],
  ["weighted", "By the weighted table only"],
];
const SKIP_TOKENS = new Set(["", "-", "_", "?", ".", "*"]);
const BONUS_PREFIX = "BONUS_";
const DEFAULT_SECTION = "DEFAULT";
const KNOWN_OPTIONS = ["questions_in_order", "answers_in_order", "answer_key_csv_path", "grade", "question_ranges", "drop_questions", "bonus_all", "multi_marked", "legend"];

// ------------------------------------------------------------------ pure helpers (tested in node)
const RANGE = /^([^.\d]+)(\d+)\.{2,3}(\d+)$/;
const NUMBERED = /^([^.\d]+)(\d+)$/;
export const MARK = /^-?\d+(\.\d+)?(\/\d+)?$/;

export function expandFields(list) {
  const out = [];
  for (const item of Array.isArray(list) ? list : [list]) {
    for (const part of String(item ?? "").split(/[,\s]+/)) {
      if (!part) continue;
      const m = RANGE.exec(part);
      if (m && Number(m[2]) < Number(m[3])) for (let i = Number(m[2]); i <= Number(m[3]); i++) out.push(`${m[1]}${i}`);
      else out.push(part);
    }
  }
  return out;
}

// ["q1","q2","q3","roll"] -> ["q1..3","roll"] (ranges only for 2+ consecutive numbers)
export function compactRanges(names) {
  const out = [];
  let run = null;
  const flush = () => {
    if (!run) return;
    out.push(run.end > run.start ? `${run.prefix}${run.start}..${run.end}` : `${run.prefix}${run.start}`);
    run = null;
  };
  for (const name of names) {
    const m = NUMBERED.exec(name);
    if (m && run && run.prefix === m[1] && Number(m[2]) === run.end + 1 && String(Number(m[2])) === m[2]) {
      run.end += 1;
      continue;
    }
    flush();
    if (m && String(Number(m[2])) === m[2]) run = { prefix: m[1], start: Number(m[2]), end: Number(m[2]) };
    else out.push(name);
  }
  flush();
  return out;
}

export function markValue(v) {
  if (typeof v === "number") return String(v);
  return String(v ?? "").trim().replace(/−/g, "-").replace(/\s+/g, "");
}

export function markNumber(v) {
  const s = markValue(v);
  if (!MARK.test(s)) return NaN;
  if (s.includes("/")) {
    const [a, b] = s.split("/");
    return Number(b) ? Number(a) / Number(b) : NaN;
  }
  return Number(s);
}

// Answer item of evaluation.json -> {kind, value}
export function answerFromItem(item) {
  if (typeof item === "string") return { kind: "single", value: item };
  if (Array.isArray(item) && item.length && item.every((x) => typeof x === "string")) return { kind: "multi", value: [...item] };
  if (Array.isArray(item) && item.every((x) => Array.isArray(x) && x.length === 2)) return { kind: "weighted", value: item.map(([a, s]) => [String(a), markValue(s)]) };
  return null;
}

export function itemFromAnswer(answer) {
  if (!answer) return null;
  if (answer.kind === "single") return answer.value ? answer.value : null;
  if (answer.kind === "multi") {
    const values = [...new Set(answer.value.filter(Boolean))];
    if (!values.length) return null;
    return values.length === 1 ? values[0] : values;
  }
  if (answer.kind === "weighted") {
    const rows = answer.value.filter(([a]) => a);
    if (!rows.length) return null;
    // whole numbers as numbers; fractions and decimals stay text (the engine parses them)
    return rows.map(([a, s]) => [a, /^-?\d+$/.test(markValue(s)) ? Number(markValue(s)) : markValue(s)]);
  }
  return null;
}

// "A=1, B=2, AB=3" <-> [["A","1"],["B","2"],["AB","3"]]
export function weightsToText(rows) {
  return rows.map(([a, s]) => `${a}=${s}`).join(", ");
}
export function weightsFromText(text) {
  const rows = [];
  const errors = [];
  for (const part of String(text || "").split(/[,;]+/)) {
    const t = part.trim();
    if (!t) continue;
    const m = /^([^=:\s]+)\s*[=:]\s*(\S+)$/.exec(t);
    if (!m || !MARK.test(markValue(m[2]))) errors.push(t);
    else rows.push([m[1], markValue(m[2])]);
  }
  return { rows, errors };
}

function answerOfToken(token) {
  const parts = token.split(/[|/+&]/).filter(Boolean);
  if (parts.length > 1) return { kind: "multi", value: parts };
  return { kind: "single", value: token };
}

// Pasted answers -> list (null = skip). "ACBD", "A C B D", "A,B,AB", "A|B" (either), "-" skip.
export function parseAnswerString(text, singleChar = true) {
  const s = String(text || "").trim();
  if (!s) return [];
  const hasSeparators = /[\s,;\t]/.test(s);
  // one character per question; "B|D" (either) stays together
  const tokens = hasSeparators || !singleChar ? s.split(/[\s,;\t]+/) : s.match(/[^|/+&](?:[|/+&][^|/+&])*/g) || [];
  return tokens.map((t) => (SKIP_TOKENS.has(t.trim()) ? null : answerOfToken(t.trim())));
}

// One answer cell from a CSV / Excel key: "A", "A,B" (either, like the engine's CSV), "[['A',1],...]"
export function answerFromCell(cell) {
  const s = String(cell ?? "").trim();
  if (SKIP_TOKENS.has(s)) return null;
  if (s.startsWith("[")) {
    try {
      const parsed = answerFromItem(JSON.parse(s.replace(/'/g, '"')));
      if (parsed) return parsed;
    } catch (e) {
      /* fall through */
    }
  }
  if (s.includes(",")) return { kind: "multi", value: s.split(",").map((x) => x.trim()).filter(Boolean) };
  return answerOfToken(s);
}

// Rows of a key file -> {answers: {q: answer}, ordered: [answer...]} (one of them filled)
export function answersFromRows(rows, questions) {
  const known = new Set(questions);
  const clean = rows.filter((r) => r.some((c) => String(c).trim()));
  const pairs = clean.filter((r) => r.length >= 2 && known.has(String(r[0]).trim()));
  if (pairs.length && pairs.length >= clean.length / 2) {
    const answers = {};
    for (const r of pairs) answers[String(r[0]).trim()] = answerFromCell(r[1]);
    return { answers, ordered: null, matched: pairs.length };
  }
  // Header row of question names, answers in the next row
  if (clean.length >= 2 && clean[0].filter((c) => known.has(String(c).trim())).length >= Math.max(1, clean[0].length / 2)) {
    const answers = {};
    clean[0].forEach((c, i) => {
      if (known.has(String(c).trim())) answers[String(c).trim()] = answerFromCell(clean[1][i]);
    });
    return { answers, ordered: null, matched: Object.keys(answers).length };
  }
  // A single row or a single column of answers, in question order
  const flat = clean.length === 1 ? clean[0] : clean.every((r) => r.length <= 2) ? clean.map((r) => r[r.length - 1]) : null;
  if (!flat) return { answers: {}, ordered: null, matched: 0 };
  return { answers: null, ordered: flat.map(answerFromCell), matched: flat.length };
}

// evaluation.json -> editable model. labels: graded candidates (template field order).
export function modelFromEvaluation(evaluation, labels) {
  const ev = evaluation && typeof evaluation === "object" ? evaluation : null;
  const options = (ev && ev.options) || {};
  const schemes = (ev && ev.marking_schemes) || {};
  const def = schemes[DEFAULT_SECTION] || PRESETS[0];
  const model = {
    exists: !!ev,
    source: ev && ev.source_type === "csv" ? "csv" : "custom",
    csvPath: options.answer_key_csv_path || "",
    questions: [],
    answers: {},
    scheme: { correct: markValue(def.correct), incorrect: markValue(def.incorrect), unmarked: markValue(def.unmarked) },
    sections: [],
    drop: new Set(expandFields(options.drop_questions || [])),
    bonus: new Set(expandFields(options.bonus_all || [])),
    multi: options.multi_marked || "default",
    grade: options.grade !== false,
    ranges: (options.question_ranges || []).join(", "),
    legend: (options.legend || []).map((b) => ({ min: b.min ?? 0, label: b.label || "", color: b.color })),
    extraOptions: {},
    extraTop: {},
  };
  for (const [k, v] of Object.entries(options)) if (!KNOWN_OPTIONS.includes(k)) model.extraOptions[k] = v;
  for (const [k, v] of Object.entries(ev || {})) if (!["source_type", "options", "marking_schemes"].includes(k)) model.extraTop[k] = v;
  if (model.source === "custom" && ev) {
    model.questions = expandFields(options.questions_in_order || []);
    (options.answers_in_order || []).forEach((item, i) => {
      const q = model.questions[i];
      if (q !== undefined) model.answers[q] = answerFromItem(item);
    });
  } else if (model.source === "csv") {
    // answers live in the CSV file; questions_in_order (if any) is kept as written
    model.csvQuestions = options.questions_in_order || null;
  } else {
    const q = labels.filter((l) => /^q\d/i.test(l));
    model.questions = q.length ? q : [...labels];
  }
  for (const [name, s] of Object.entries(schemes)) {
    if (name === DEFAULT_SECTION || !s || !s.marking) continue;
    const bonus = name.startsWith(BONUS_PREFIX);
    model.sections.push({
      name: bonus ? name.slice(BONUS_PREFIX.length) : name,
      questions: (Array.isArray(s.questions) ? s.questions : [s.questions]).join(", "),
      correct: markValue(s.marking.correct),
      incorrect: markValue(s.marking.incorrect),
      unmarked: markValue(s.marking.unmarked),
      bonus,
    });
  }
  return model;
}

// Model -> {evaluation, problems: [text], skipped: [questions without an answer]}
export function evaluationFromModel(model) {
  const problems = [];
  const options = { ...model.extraOptions };
  const skipped = [];
  if (model.source === "csv") {
    options.answer_key_csv_path = model.csvPath;
    if (model.csvQuestions) options.questions_in_order = model.csvQuestions;
  } else {
    const questions = [];
    const answers = [];
    for (const q of model.questions) {
      const item = itemFromAnswer(model.answers[q]);
      if (item === null) skipped.push(q);
      else {
        questions.push(q);
        answers.push(item);
      }
    }
    if (!questions.length) problems.push("Set the correct answer of at least one question");
    options.questions_in_order = compactRanges(questions);
    options.answers_in_order = answers;
  }
  if (!model.grade) options.grade = false;
  const ranges = expandFields(model.ranges);
  if (ranges.length) options.question_ranges = compactRanges(ranges);
  const scored = new Set(model.questions);
  const drop = [...model.drop].filter((q) => scored.has(q) || model.source === "csv");
  const bonus = [...model.bonus].filter((q) => (scored.has(q) || model.source === "csv") && !model.drop.has(q));
  if (drop.length) options.drop_questions = compactRanges(drop);
  if (bonus.length) options.bonus_all = compactRanges(bonus);
  if (model.multi && model.multi !== "default") options.multi_marked = model.multi;
  const legend = model.legend.filter((b) => String(b.label).trim());
  if (legend.length) options.legend = legend.map((b) => ({ min: Number(b.min) || 0, label: String(b.label).trim(), ...(b.color ? { color: b.color } : {}) }));

  const markingOf = (s, where) => {
    const out = {};
    for (const k of ["correct", "incorrect", "unmarked"]) {
      const v = markValue(s[k]);
      if (!MARK.test(v)) problems.push(`${where}: '${s[k]}' is not a number or fraction for ${k}`);
      out[k] = v;
    }
    return out;
  };
  const schemes = { [DEFAULT_SECTION]: markingOf(model.scheme, "Marking scheme") };
  const seen = new Set();
  const answered = new Set(model.source === "csv" ? model.questions : model.questions.filter((q) => !skipped.includes(q)));
  for (const s of model.sections) {
    const name = String(s.name || "").trim().replace(/\s+/g, "_");
    if (!name) {
      problems.push("Every section needs a name");
      continue;
    }
    const key = (s.bonus ? BONUS_PREFIX : "") + name;
    if (key === DEFAULT_SECTION || schemes[key]) {
      problems.push(`Section name '${name}' is used twice`);
      continue;
    }
    let qs = expandFields(s.questions);
    if (model.source !== "csv") qs = qs.filter((q) => answered.has(q));
    const overlap = qs.filter((q) => seen.has(q));
    if (overlap.length) problems.push(`Section ${name}: ${compactRanges(overlap).join(", ")} already belong to another section`);
    qs.forEach((q) => seen.add(q));
    if (!qs.length) {
      problems.push(`Section ${name} has no answered questions`);
      continue;
    }
    const marking = markingOf(s, `Section ${name}`);
    if (s.bonus) marking.incorrect = marking.unmarked = marking.correct;
    schemes[key] = { questions: compactRanges(qs), marking };
  }
  const evaluation = { ...model.extraTop, source_type: model.source, options, marking_schemes: schemes };
  return { evaluation, problems, skipped };
}

// ------------------------------------------------------------------ GUI
const help = (text) => el("i", { class: "help", tabindex: "0", "aria-label": text }, "ⓘ", el("span", { class: "help-text" }, text));
const labelled = (text, helpText, control) => el("label", { class: "field has-help" }, el("span", {}, text, helpText ? help(helpText) : null), control);

class ScoringPanel {
  constructor(editor) {
    this.editor = editor;
    this.fieldValues = {};
    this.blocks = [];
    for (const name of Object.keys(editor.doc.fieldBlocks || {})) {
      const info = editor.blockInfo(name);
      this.blocks.push({ name, labels: info.labels });
      for (const label of info.labels) this.fieldValues[label] = info.values.map(String);
    }
    this.labels = this.blocks.flatMap((b) => b.labels);
    let evaluation = null;
    if (editor.evaluationText && editor.evaluationText.trim()) {
      try {
        evaluation = JSON.parse(editor.evaluationText);
      } catch (e) {
        toast("evaluation.json is not valid JSON: fix it in Edit JSON first", "error", 6000);
        this.broken = true;
      }
    }
    this.model = modelFromEvaluation(evaluation, this.labels);
    this.startText = JSON.stringify(evaluationFromModel(this.model).evaluation);
    this.selected = new Set();
    this.lastClicked = null;
    this.sample = null;
    this.preview = null;
  }

  open() {
    if (this.broken) return;
    this.left = el("div", { class: "sc-left" });
    this.right = el("div", { class: "sc-right" });
    this.problemsEl = el("span", { class: "muted small" });
    this.applyBtn = el("button", { class: "primary", onclick: () => this.apply() }, "Apply");
    this.backdrop = el(
      "div",
      { class: "fs-backdrop" },
      el(
        "div",
        { class: "fs-panel", role: "dialog", "aria-label": "Scoring" },
        el(
          "div",
          { class: "fs-head" },
          el("h2", {}, "Scoring"),
          el("span", { class: "muted small" }, "Answer key and marking scheme (evaluation.json)"),
          el("span", { class: "spacer" }),
          el("button", { class: "small danger", title: "Remove the answer key: sheets are read but not scored", onclick: () => this.removeAll() }, "Remove scoring"),
          el("button", { class: "small ghost", title: "Close (Esc)", onclick: () => this.close() }, "✕")
        ),
        el("div", { class: "sc-body" }, this.left, this.right),
        el("div", { class: "fs-foot" }, this.problemsEl, el("span", { class: "spacer" }), el("button", { class: "ghost", onclick: () => this.close() }, "Cancel"), this.applyBtn)
      )
    );
    this.backdrop.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Escape") this.close();
    });
    document.body.append(this.backdrop);
    this.render();
  }

  changed() {
    return JSON.stringify(evaluationFromModel(this.model).evaluation) !== this.startText;
  }

  close(force = false) {
    if (!force && this.changed() && !confirm("Close without applying the scoring changes?")) return;
    this.backdrop.remove();
  }

  rerender() {
    const top = this.left.scrollTop;
    this.render();
    this.left.scrollTop = top;
  }

  render() {
    this.left.innerHTML = "";
    this.right.innerHTML = "";
    this.left.append(this.renderQuestions(), this.renderKey());
    this.right.append(this.renderScheme(), this.renderSections(), this.renderRules(), this.renderLegend(), this.renderPreview());
    const { problems, skipped } = evaluationFromModel(this.model);
    const notes = [...problems];
    if (skipped.length && this.model.source === "custom") notes.push(`${skipped.length} question(s) have no answer and are not scored`);
    this.problemsEl.textContent = notes.join(" · ");
    this.problemsEl.style.color = problems.length ? "var(--err)" : "";
    this.applyBtn.disabled = problems.length > 0;
  }

  // ---------------------------------------------------------------- questions
  renderQuestions() {
    const m = this.model;
    const box = el("div", { class: "sc-section" }, el("h3", {}, "Questions"));
    const gradeChk = el("input", { type: "checkbox", checked: m.grade, onchange: (e) => ((m.grade = e.target.checked), this.render()) });
    box.append(
      el("label", { class: "row gap has-help" }, gradeChk, "Score the sheets", help("Untick to keep the answer key but read sheets without scoring them (\"grade\": false).")),
      m.source === "csv"
        ? el(
            "div",
            { class: "muted small" },
            `Answers come from the file ${m.csvPath}. `,
            el("button", { class: "small", title: "Read that file into the grid below so the answers can be edited here", onclick: () => this.loadCsvKey() }, "Load into the grid")
          )
        : ""
    );
    const ranges = el("input", { type: "text", value: m.ranges, placeholder: "all", style: { width: "100%" } });
    ranges.addEventListener("change", () => ((m.ranges = ranges.value), this.render()));
    const rangesField = labelled("Only grade these (question ranges)", "Optional limit, e.g. q1..50. Useful when the key comes from a master sheet or a CSV that also holds other fields. Empty = all graded questions.", ranges);
    if (m.source === "csv") {
      box.append(rangesField);
      return box;
    }
    const qInput = el("input", { type: "text", value: compactRanges(m.questions).join(", "), placeholder: "q1..50", style: { width: "100%" } });
    qInput.addEventListener("change", () => {
      const list = expandFields(qInput.value);
      const unknown = list.filter((q) => !this.fieldValues[q]);
      if (unknown.length) toast(`Not fields of this template: ${compactRanges(unknown).join(", ")}`, "error", 6000);
      m.questions = list.filter((q) => this.fieldValues[q]);
      this.render();
    });
    box.append(
      labelled("Graded questions", "The fields scored as questions, e.g. q1..50. Roll numbers, phone numbers and other fields stay out of the answer key.", qInput),
      el(
        "div",
        { class: "row gap wrap small" },
        "Add a block:",
        this.blocks.map((b) =>
          el(
            "button",
            {
              class: "small",
              title: `Add ${b.labels.length} fields of block ${b.name}`,
              onclick: () => {
                const have = new Set(m.questions);
                m.questions = this.labels.filter((l) => have.has(l) || b.labels.includes(l));
                this.render();
              },
            },
            b.name
          )
        )
      )
    );
    box.append(rangesField);
    return box;
  }

  // ---------------------------------------------------------------- answer key grid
  renderKey() {
    const m = this.model;
    const box = el("div", { class: "sc-section" }, el("h3", {}, "Answer key"));
    if (m.source === "csv") {
      box.append(el("p", { class: "muted small" }, "The answers are in a CSV file. Load it into the grid, paste a row of answers, or import a file to edit them here."));
    }
    const paste = el("input", { type: "text", placeholder: "e.g. ACBDDA… or A, B, AB, C", style: { flex: 1 } });
    const fileInput = el("input", { type: "file", accept: ".csv,.tsv,.txt,.xlsx,.xlsm", hidden: true, onchange: (e) => this.importFile(e.target.files[0]) });
    const masterInput = el("input", { type: "file", accept: "image/*,.pdf", hidden: true, onchange: (e) => this.scanMaster(e.target.files[0]) });
    box.append(
      el(
        "div",
        { class: "row gap wrap has-help" },
        paste,
        el("button", { class: "small", onclick: () => this.pasteAnswers(paste.value) }, "Fill"),
        help("Answers in question order, starting at the first selected question (or the first question). One letter per question, or separated by spaces or commas. A|B accepts either; - leaves a question unchanged.")
      ),
      el(
        "div",
        { class: "row gap wrap", style: { margin: "6px 0" } },
        el("button", { class: "small", title: "CSV or Excel: rows of question,answer — or one row / column of answers in order", onclick: () => fileInput.click() }, "Import CSV / Excel…"),
        el("button", { class: "small", title: "Read a master sheet filled with the correct answers (uses the saved layout)", onclick: () => masterInput.click() }, "Scan a master sheet…"),
        el("button", { class: "small ghost", onclick: () => this.clearAnswers() }, "Clear answers"),
        fileInput,
        masterInput
      ),
      el("p", { class: "muted small" }, "Click an option to set the answer; Ctrl-click to accept several. Tick rows (Shift for a range) to make a section.")
    );
    const head = el("tr", {}, el("th", {}, ""), el("th", {}, "Q"), el("th", {}, "Correct answer"), el("th", { title: "Key: one or several correct options. Weighted: marks per option" }, "Type"), el("th", { title: "Dropped: 0 marks for everyone, not counted in the maximum" }, "Drop"), el("th", { title: "Bonus: full marks for everyone" }, "Bonus"), el("th", {}, "Section"));
    const sectionOf = this.sectionMap();
    const rows = m.questions.map((q, index) => this.renderRow(q, index, sectionOf[q]));
    box.append(el("table", { class: "sc-grid" }, el("thead", {}, head), el("tbody", {}, rows)));
    if (!m.questions.length && m.source !== "csv") box.append(el("p", { class: "muted" }, "No graded questions: set them above."));
    return box;
  }

  sectionMap() {
    const map = {};
    for (const s of this.model.sections) for (const q of expandFields(s.questions)) map[q] = s.name;
    return map;
  }

  renderRow(q, index, section) {
    const m = this.model;
    const answer = m.answers[q] || null;
    const options = this.fieldValues[q] || [];
    const pick = (value, e) => {
      const current = m.answers[q];
      if (e.ctrlKey || e.metaKey) {
        const list = current ? (current.kind === "multi" ? [...current.value] : current.kind === "single" ? [current.value] : []) : [];
        const i = list.indexOf(value);
        if (i >= 0) list.splice(i, 1);
        else list.push(value);
        m.answers[q] = list.length > 1 ? { kind: "multi", value: list } : list.length ? { kind: "single", value: list[0] } : null;
      } else if (current && current.kind === "single" && current.value === value) m.answers[q] = null;
      else m.answers[q] = { kind: "single", value };
      this.rerender();
    };
    const isOn = (v) => answer && ((answer.kind === "single" && answer.value === v) || (answer.kind === "multi" && answer.value.includes(v)));
    let cell;
    if (answer && answer.kind === "weighted") {
      const input = el("input", { type: "text", value: weightsToText(answer.value), style: { width: "100%" }, title: "Marks per marked answer, e.g. A=1, B=2, AB=3 (fractions allowed)" });
      input.addEventListener("change", () => {
        const { rows, errors } = weightsFromText(input.value);
        if (errors.length) toast(`${q}: can't read ${errors.join(", ")} (use A=1, AB=3/2)`, "error", 6000);
        m.answers[q] = { kind: "weighted", value: rows };
        this.rerender();
      });
      cell = input;
    } else {
      const extra = answer && answer.kind === "single" && !options.includes(answer.value) ? answer.value : null;
      cell = el(
        "div",
        { class: "row wrap" },
        options.map((v) => el("button", { class: `sc-opt${isOn(v) ? " on" : ""}${answer && answer.kind === "multi" ? " multi" : ""}`, title: "Click: the answer · Ctrl-click: also accept", onclick: (e) => pick(v, e) }, v)),
        extra ? el("span", { class: "chip", title: "Answer that is not one of the bubbles (e.g. a combination)" }, extra) : null,
        answer && answer.kind === "multi" ? el("span", { class: "muted small" }, " any of " + answer.value.join(", ")) : null,
        !answer ? el("span", { class: "sc-missing small" }, " no answer") : null
      );
    }
    const typeSel = el(
      "select",
      {
        class: "small",
        onchange: (e) => {
          if (e.target.value === "weighted") {
            const base = answer ? (answer.kind === "single" ? [answer.value] : answer.kind === "multi" ? answer.value : []) : [];
            m.answers[q] = { kind: "weighted", value: base.map((v) => [v, m.scheme.correct || "1"]) };
          } else {
            const first = answer && answer.kind === "weighted" ? answer.value.map(([a]) => a) : [];
            m.answers[q] = first.length > 1 ? { kind: "multi", value: first } : first.length ? { kind: "single", value: first[0] } : null;
          }
          this.rerender();
        },
      },
      el("option", { value: "key", selected: !answer || answer.kind !== "weighted" }, "Key"),
      el("option", { value: "weighted", selected: !!answer && answer.kind === "weighted" }, "Weighted")
    );
    const toggle = (set) =>
      el("input", {
        type: "checkbox",
        checked: set.has(q),
        onchange: (e) => {
          e.target.checked ? set.add(q) : set.delete(q);
          this.rerender();
        },
      });
    const pickRow = el("input", {
      type: "checkbox",
      checked: this.selected.has(q),
      title: "Select (Shift-click for a range)",
      onclick: (e) => {
        if (e.shiftKey && this.lastClicked !== null) {
          const [a, b] = [Math.min(this.lastClicked, index), Math.max(this.lastClicked, index)];
          for (let i = a; i <= b; i++) this.selected.add(m.questions[i]);
        } else e.target.checked ? this.selected.add(q) : this.selected.delete(q);
        this.lastClicked = index;
        this.rerender();
      },
    });
    const dropped = m.drop.has(q);
    return el("tr", { class: dropped ? "dropped" : "" }, el("td", {}, pickRow), el("td", { class: "mono" }, q), el("td", {}, cell), el("td", {}, typeSel), el("td", {}, toggle(m.drop)), el("td", {}, toggle(m.bonus)), el("td", { class: "muted small" }, section || ""));
  }

  questionsFromSelection() {
    const start = this.selected.size ? this.model.questions.findIndex((q) => this.selected.has(q)) : 0;
    return this.model.questions.slice(Math.max(0, start));
  }

  singleCharOptions(questions) {
    return questions.every((q) => (this.fieldValues[q] || []).every((v) => String(v).length === 1));
  }

  pasteAnswers(text) {
    const targets = this.questionsFromSelection();
    const list = parseAnswerString(text, this.singleCharOptions(targets));
    if (!list.length) return;
    let n = 0;
    list.forEach((answer, i) => {
      if (i < targets.length && answer) {
        this.model.answers[targets[i]] = answer;
        n++;
      }
    });
    if (list.length > targets.length) toast(`${list.length - targets.length} extra answer(s) ignored`, "error");
    toast(`Filled ${n} answer(s)`, "ok");
    this.rerender();
  }

  clearAnswers() {
    if (!confirm("Clear every answer in the grid?")) return;
    this.model.answers = {};
    this.rerender();
  }

  useRows(rows, source) {
    const m = this.model;
    const { answers, ordered, matched } = answersFromRows(rows, this.labels);
    if (!matched) return toast(`${source}: no answers found (expected rows of question,answer or one row of answers)`, "error", 6000);
    if (m.source === "csv") {
      m.source = "custom";
      delete m.extraOptions.answer_key_image_path;
    }
    if (answers) {
      const have = new Set(m.questions);
      for (const [q, a] of Object.entries(answers)) {
        m.answers[q] = a;
        have.add(q);
      }
      m.questions = this.labels.filter((l) => have.has(l));
    } else {
      const targets = this.questionsFromSelection();
      ordered.forEach((a, i) => {
        if (i < targets.length && a) m.answers[targets[i]] = a;
      });
    }
    toast(`${source}: ${matched} answer(s) read`, "ok");
    this.render();
  }

  async importFile(file) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file, file.name);
    try {
      const data = await api("/templates/answer-key/parse", { method: "POST", form });
      this.useRows(data.rows || [], file.name);
    } catch (error) {
      toast(error.message, "error", 6000);
    }
  }

  async loadCsvKey() {
    const path = this.model.csvPath;
    try {
      const response = await api(`/templates/${encodeURIComponent(this.editor.id)}/files/${path.split("/").map(encodeURIComponent).join("/")}`, { raw: true });
      const blob = await response.blob();
      const form = new FormData();
      form.append("file", blob, path.split("/").pop() || "key.csv");
      const data = await api("/templates/answer-key/parse", { method: "POST", form });
      this.useRows(data.rows || [], path);
    } catch (error) {
      toast(`Could not read ${path}: ${error.message}`, "error", 6000);
    }
  }

  async readSheet(file) {
    if (this.editor.dirty) toast("Reading with the last saved layout (the editor has unsaved changes)", "", 5000);
    const form = new FormData();
    form.append("file", file, file.name);
    return api(`/templates/${encodeURIComponent(this.editor.id)}/read-sheet`, { method: "POST", form });
  }

  async scanMaster(file) {
    if (!file) return;
    try {
      toast("Reading the master sheet…");
      const data = await this.readSheet(file);
      if (data.status === "error") return toast(data.error || "The sheet could not be read", "error", 6000);
      const empty = this.editor.doc.emptyValue ?? "";
      const m = this.model;
      if (m.source === "csv") m.source = "custom";
      let n = 0;
      const blanks = [];
      for (const q of m.questions) {
        const value = String(data.responses[q] ?? "");
        if (!value || value === empty) {
          blanks.push(q);
          continue;
        }
        const single = this.singleCharOptions([q]);
        m.answers[q] = single && value.length > 1 ? { kind: "multi", value: [...value] } : { kind: "single", value };
        n++;
      }
      const flagged = (data.review || []).map((r) => r.name).filter((name) => m.questions.includes(name));
      toast(`Master sheet: ${n} answer(s) set${blanks.length ? `, ${blanks.length} blank` : ""}${flagged.length ? `; check ${compactRanges(flagged).join(", ")}` : ""}`, flagged.length || blanks.length ? "" : "ok", 8000);
      this.render();
    } catch (error) {
      toast(error.message, "error", 6000);
    }
  }

  // ---------------------------------------------------------------- marking
  markInputs(target, onchange) {
    return ["correct", "incorrect", "unmarked"].map((k) => {
      const input = el("input", { type: "text", class: "mark", value: target[k] ?? "", title: "A number or a fraction such as -1/4", disabled: target.bonus && k !== "correct" });
      input.addEventListener("change", () => {
        target[k] = markValue(input.value);
        if (!MARK.test(target[k])) toast(`'${input.value}' is not a number or fraction`, "error");
        onchange();
      });
      return el("label", { class: "field" }, { correct: "Correct", incorrect: "Wrong", unmarked: "Unmarked" }[k], input);
    });
  }

  renderScheme() {
    const m = this.model;
    return el(
      "div",
      { class: "sc-section" },
      el("h3", {}, "Marking scheme", help("Marks for a correct, a wrong and an unmarked answer. Negative marks and fractions are allowed, e.g. -1/4.")),
      el("div", { class: "sc-row" }, this.markInputs(m.scheme, () => this.render())),
      el(
        "div",
        { class: "row gap wrap small" },
        "Presets:",
        PRESETS.map((p) => el("button", { class: "small", onclick: () => ((m.scheme = { correct: p.correct, incorrect: p.incorrect, unmarked: p.unmarked }), this.render()) }, p.label))
      )
    );
  }

  renderSections() {
    const m = this.model;
    const box = el("div", { class: "sc-section" }, el("h3", {}, "Sections", help("A range of questions with its own marking scheme. Tick Bonus to give full marks to everyone in that section. Per-section scores appear in Results and as export columns.")));
    m.sections.forEach((s, i) => {
      const name = el("input", { type: "text", value: s.name, style: { width: "120px" } });
      name.addEventListener("change", () => ((s.name = name.value.trim()), this.render()));
      const qs = el("input", { type: "text", value: s.questions, placeholder: "q1..10", style: { width: "140px" } });
      qs.addEventListener("change", () => ((s.questions = qs.value), this.render()));
      const bonus = el("input", { type: "checkbox", checked: s.bonus, onchange: (e) => ((s.bonus = e.target.checked), this.render()) });
      box.append(
        el(
          "div",
          { class: "sc-row", style: { borderBottom: "1px solid var(--border)", marginBottom: "6px" } },
          el("label", { class: "field" }, "Name", name),
          el("label", { class: "field" }, "Questions", qs),
          this.markInputs(s, () => this.render()),
          el("label", { class: "field" }, "Bonus", bonus),
          el("button", { class: "small ghost", title: "Remove this section", onclick: () => (m.sections.splice(i, 1), this.render()) }, "✕")
        )
      );
    });
    box.append(
      el(
        "div",
        { class: "row gap wrap" },
        el(
          "button",
          {
            class: "small",
            title: "New section from the ticked rows of the answer key",
            onclick: () => {
              const qs = m.questions.filter((q) => this.selected.has(q));
              if (!qs.length) return toast("Tick the questions of the section in the answer key first", "error");
              let n = m.sections.length + 1;
              while (m.sections.some((s) => s.name === `Section${n}`)) n++;
              m.sections.push({ name: `Section${n}`, questions: compactRanges(qs).join(", "), ...m.scheme, bonus: false });
              this.selected.clear();
              this.render();
            },
          },
          "+ Section from ticked rows"
        ),
        el("button", { class: "small", onclick: () => (m.sections.push({ name: `Section${m.sections.length + 1}`, questions: "", ...m.scheme, bonus: false }), this.render()) }, "+ Empty section")
      )
    );
    return box;
  }

  renderRules() {
    const m = this.model;
    const sel = el("select", { onchange: (e) => ((m.multi = e.target.value), this.render()) }, MULTI_RULES.map(([v, t]) => el("option", { value: v, selected: m.multi === v }, t)));
    const setInput = (set, placeholder) => {
      const input = el("input", { type: "text", value: compactRanges([...set]).join(", "), placeholder, style: { width: "100%" } });
      input.addEventListener("change", () => {
        set.clear();
        expandFields(input.value).forEach((q) => set.add(q));
        this.render();
      });
      return input;
    };
    return el(
      "div",
      { class: "sc-section" },
      el("h3", {}, "Special questions"),
      labelled("Score multi-marked answers", "When a student marks more than one bubble (e.g. AB for a one-letter answer). 'Use the answer key' gives the weighted marks if AB is listed, else marks it wrong.", sel),
      labelled("Dropped questions", "0 marks for everyone and left out of the maximum score, e.g. a misprinted question. Same as the Drop column.", setInput(m.drop, "none")),
      labelled("Bonus for everyone", "Full marks for every student, whatever they marked. Same as the Bonus column.", setInput(m.bonus, "none"))
    );
  }

  renderLegend() {
    const m = this.model;
    const box = el("div", { class: "sc-section" }, el("h3", {}, "Score legend", help("Optional bands such as Fail below 40, Pass from 40, Distinction from 75. The band appears next to the score in Results and in exports.")));
    m.legend.forEach((b, i) => {
      const min = el("input", { type: "number", value: b.min, step: "any", style: { width: "80px" } });
      min.addEventListener("change", () => ((b.min = Number(min.value) || 0), this.render()));
      const label = el("input", { type: "text", value: b.label, style: { width: "140px" } });
      label.addEventListener("change", () => ((b.label = label.value), this.render()));
      box.append(el("div", { class: "sc-row" }, el("label", { class: "field" }, "From score", min), el("label", { class: "field" }, "Label", label), el("button", { class: "small ghost", onclick: () => (m.legend.splice(i, 1), this.render()) }, "✕")));
    });
    box.append(el("button", { class: "small", onclick: () => (m.legend.push({ min: 0, label: "" }), this.render()) }, "+ Band"));
    return box;
  }

  // ---------------------------------------------------------------- preview
  renderPreview() {
    const box = el("div", { class: "sc-section" }, el("h3", {}, "Preview", help("Score a sample sheet with this draft before running a batch. The sheet is read with the saved layout; nothing is stored.")));
    const input = el("input", { type: "file", accept: "image/*,.pdf", hidden: true, onchange: (e) => this.loadSample(e.target.files[0]) });
    box.append(
      el(
        "div",
        { class: "row gap wrap" },
        el("button", { class: "small", onclick: () => input.click() }, this.sample ? "Another sheet…" : "Score a sample sheet…"),
        this.sample ? el("button", { class: "small", onclick: () => this.scorePreview() }, "Score again") : null,
        this.sample ? el("span", { class: "muted small" }, this.sample.file_id) : null,
        input
      )
    );
    const p = this.preview;
    if (p && p.error) box.append(el("div", { class: "chip error" }, p.error));
    else if (p) {
      box.append(scoreSummary(p) || "");
      box.append(
        el(
          "table",
          { class: "table small" },
          el("thead", {}, el("tr", {}, el("th", {}, "Q"), el("th", {}, "Marked"), el("th", {}, "Answer"), el("th", {}, "Verdict"), el("th", {}, "Marks"), el("th", {}, "Section"))),
          el(
            "tbody",
            {},
            (p.questions || []).map((r) =>
              el(
                "tr",
                {},
                el("td", { class: "mono" }, r.question),
                el("td", { class: "mono" }, r.marked === "" ? "—" : r.marked),
                el("td", { class: "mono" }, typeof r.answer === "string" ? r.answer : JSON.stringify(r.answer)),
                el("td", { class: `verdict-${r.verdict}` }, r.verdict),
                el("td", {}, String(Math.round(r.delta * 100) / 100)),
                el("td", { class: "muted" }, r.section === "DEFAULT" ? "" : r.section)
              )
            )
          )
        )
      );
    }
    return box;
  }

  async loadSample(file) {
    if (!file) return;
    try {
      toast("Reading the sample sheet…");
      const data = await this.readSheet(file);
      if (data.status === "error") throw new Error(data.error || "The sheet could not be read");
      this.sample = data;
      await this.scorePreview();
    } catch (error) {
      this.preview = { error: error.message };
      this.render();
    }
  }

  async scorePreview() {
    const { evaluation, problems } = evaluationFromModel(this.model);
    if (problems.length) {
      this.preview = { error: problems.join("; ") };
      return this.render();
    }
    try {
      this.preview = await api(`/templates/${encodeURIComponent(this.editor.id)}/score-preview`, { method: "POST", json: { evaluation, responses: this.sample.responses } });
    } catch (error) {
      this.preview = { error: (error.errors || []).map((e) => e.message).join("; ") || error.message };
    }
    this.render();
  }

  // ---------------------------------------------------------------- apply
  async apply() {
    const { evaluation, problems } = evaluationFromModel(this.model);
    if (problems.length) return toast(problems[0], "error", 6000);
    let config;
    try {
      config = this.editor.configText.trim() ? JSON.parse(this.editor.configText) : null;
    } catch (e) {
      config = undefined;
    }
    try {
      const body = { template: this.editor.doc, evaluation, clear: config === null ? ["config"] : [] };
      if (config) body.config = config;
      const result = await api(`/templates/${encodeURIComponent(this.editor.id)}/validate`, { method: "POST", json: body });
      const errors = result.evaluation || [];
      if (errors.length) {
        this.problemsEl.textContent = errors.map((e) => (e.path && e.path !== "$root" ? `${e.path}: ` : "") + e.message).join(" · ");
        this.problemsEl.style.color = "var(--err)";
        return toast("The engine refused this answer key: see the message at the bottom", "error", 6000);
      }
    } catch (error) {
      /* server checks unavailable: the local checks above still apply */
    }
    this.editor.applyFiles({ evaluationText: JSON.stringify(evaluation, null, 2) });
    this.close(true);
    toast("Scoring applied: Save to keep it, Ctrl+Z to undo", "ok");
  }

  removeAll() {
    if (!this.model.exists && !this.changed()) return this.close(true);
    if (!confirm("Remove the answer key and marking scheme? Sheets will be read but not scored.")) return;
    this.editor.applyFiles({ evaluationText: "" });
    this.close(true);
    toast("Scoring removed: Save to keep it, Ctrl+Z to undo", "ok");
  }
}

export function openScoring(editor) {
  const panel = new ScoringPanel(editor);
  panel.open();
  return panel;
}
