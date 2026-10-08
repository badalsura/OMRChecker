// Template editor: value validation ("validate") and cross-field checks
// ("checks"), built from checkboxes, dropdowns and number boxes only.
import { el, modal, toast } from "./api.js";
import { help } from "./editor_help.js";

export const PATTERN_PRESETS = [
  ["", "Any value"],
  ["[0-9]+", "Digits only"],
  ["[A-Za-z]+", "Letters only"],
  ["[A-Za-z]+[0-9]+", "Letters then digits"],
  ["[A-Za-z0-9]+", "Letters and digits"],
  ["[0-9 ]+", "Digits and spaces"],
];

// Normalizers of a check; "nozeros" is stored as a regex object
const STRIP_ZEROS = { regex: "0*([0-9]+)", group: 1 };
export const NORMALIZE_CHOICES = [
  ["none", "none"],
  ["strip", "trim spaces"],
  ["digits", "digits only"],
  ["nozeros", "strip leading zeros"],
  ["upper", "ignore case"],
  ["alnum", "letters and digits only"],
];

export function normalizeKey(spec) {
  if (spec === undefined || spec === null) return "none";
  if (typeof spec === "string") return spec;
  if (spec.regex === STRIP_ZEROS.regex && (spec.group ?? 0) === STRIP_ZEROS.group) return "nozeros";
  return "custom";
}

export function normalizeValue(key, previous) {
  if (key === "nozeros") return { ...STRIP_ZEROS };
  if (key === "custom") return previous;
  return key === "none" ? undefined : key;
}

export function normalizeDropdown(current, onchange) {
  const key = normalizeKey(current);
  const options = [...NORMALIZE_CHOICES, ...(key === "custom" ? [["custom", `custom: ${JSON.stringify(current)}`]] : [])];
  const sel = el("select", {}, options.map(([v, t]) => el("option", { value: v, selected: v === key }, t)));
  sel.addEventListener("change", () => onchange(normalizeValue(sel.value, current)));
  return sel;
}

// Preset dropdown with a "Custom" text box for anything else
export function patternControl(value, onchange) {
  value = value || "";
  const preset = PATTERN_PRESETS.some(([v]) => v === value);
  const sel = el("select", {}, [...PATTERN_PRESETS, ["__custom", "Custom…"]].map(([v, t]) => el("option", { value: v, selected: preset ? v === value : v === "__custom" }, t)));
  const custom = el("input", { value: preset ? "" : value, placeholder: "regular expression, e.g. [0-9]{7}", class: preset ? "hidden" : "" });
  sel.addEventListener("change", () => {
    if (sel.value === "__custom") {
      custom.classList.remove("hidden");
      custom.focus();
      return;
    }
    custom.classList.add("hidden");
    onchange(sel.value);
  });
  custom.addEventListener("change", () => {
    try {
      if (custom.value) new RegExp(custom.value);
      onchange(custom.value);
    } catch (e) {
      toast("That pattern is not a valid regular expression", "error");
    }
  });
  return el("div", { class: "stack" }, sel, custom);
}

const field = (label, control, ctx = "validation") => help(el("label", { class: "field" }, label, control), label, ctx);
const checkbox = (label, checked, onchange, ctx = "validation") => {
  const cb = el("input", { type: "checkbox", checked: !!checked });
  cb.addEventListener("change", () => onchange(cb.checked));
  return help(el("label", { class: "field inline-check" }, cb, ` ${label}`), label, ctx);
};
const numberBox = (value, onchange, placeholder = "") => {
  const input = el("input", { type: "number", value: value ?? "", placeholder, step: "any" });
  input.addEventListener("change", () => onchange(input.value === "" ? null : Number(input.value)));
  return input;
};

function bounds(spec) {
  if (spec === undefined || spec === null) return [null, null];
  if (Array.isArray(spec)) return [spec[0] ?? null, spec[1] ?? null];
  return [spec, spec];
}

function boundsValue(low, high) {
  if (low === null && high === null) return undefined;
  if (low !== null && low === high) return low;
  return [low, high];
}

// Count of the rules a spec sets (for the section summary)
export function ruleCount(spec) {
  if (!spec) return 0;
  return Object.keys(spec).filter((k) => k !== "onFail").length;
}

// Validation section for one or more names (all get the same rule).
// opts: {title, columns (number of columns, for the hint), open}
export function renderValidation(ed, names, opts = {}) {
  const doc = ed.doc;
  const spec = { ...((doc.validate || {})[names[0]] || {}) };
  const mixed = names.some((n) => JSON.stringify((doc.validate || {})[n] || {}) !== JSON.stringify((doc.validate || {})[names[0]] || {}));
  const key = `validate:${names.join(",")}`;
  const write = (changes) =>
    ed.edit(() => {
      const next = { ...spec, ...changes };
      for (const [k, v] of Object.entries(next)) if (v === undefined || v === null) delete next[k];
      // Defaults are left out of the JSON
      if (next.allowGaps === true) delete next.allowGaps;
      if (next.allowEmptyEnds === true) delete next.allowEmptyEnds;
      if (next.leadingZeros === "keep") delete next.leadingZeros;
      if (next.onFail === "review") delete next.onFail;
      if (next.required === false) delete next.required;
      if (Array.isArray(next.allowed) && !next.allowed.length) delete next.allowed;
      doc.validate = doc.validate || {};
      for (const n of names) {
        if (Object.keys(next).length) doc.validate[n] = { ...next };
        else delete doc.validate[n];
      }
      if (!Object.keys(doc.validate).length) delete doc.validate;
    });
  const [lenLow, lenHigh] = bounds(spec.length);
  const lenMode = spec.length === undefined ? "any" : !Array.isArray(spec.length) ? "exact" : "between";
  const lenSel = el("select", {}, [["any", "any length"], ["exact", "exactly"], ["between", "between"]].map(([v, t]) => el("option", { value: v, selected: v === lenMode }, t)));
  lenSel.addEventListener("change", () => {
    if (lenSel.value === "any") write({ length: undefined });
    else if (lenSel.value === "exact") write({ length: lenLow ?? opts.columns ?? 1 });
    else write({ length: [lenLow ?? 1, lenHigh ?? opts.columns ?? null] });
  });
  const lengthRow = el(
    "div",
    { class: "row gap" },
    lenSel,
    lenMode === "exact" ? numberBox(lenLow, (v) => write({ length: v === null ? undefined : Math.max(0, Math.round(v)) })) : null,
    lenMode === "between" ? numberBox(lenLow, (v) => write({ length: boundsValue(v, lenHigh) ?? undefined }), "min") : null,
    lenMode === "between" ? numberBox(lenHigh, (v) => write({ length: boundsValue(lenLow, v) ?? undefined }), "max") : null
  );
  const [rLow, rHigh] = bounds(spec.range);
  const allowedList = el("div", { class: "chips" });
  const allowed = (spec.allowed || []).map(String);
  for (const value of allowed) {
    allowedList.append(el("span", { class: "chip" }, value, " ", el("button", { class: "chip-x", title: "Remove", onclick: () => write({ allowed: allowed.filter((v) => v !== value) }) }, "×")));
  }
  const addAllowed = el("input", { placeholder: "add a value, Enter" });
  addAllowed.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const values = addAllowed.value.split(",").map((s) => s.trim()).filter(Boolean);
    if (values.length) write({ allowed: [...allowed, ...values.filter((v) => !allowed.includes(v))] });
  });
  const onFail = el("select", {}, [["review", "send to review"], ["blank", "blank the value"], ["both", "blank it and send to review"], ["flag", "only mark it in the results"]].map(([v, t]) => el("option", { value: v, selected: v === (spec.onFail || "review") }, t)));
  onFail.addEventListener("change", () => write({ onFail: onFail.value }));
  const leading = el("select", {}, [["keep", "keep (0123 is fine)"], ["forbid", "forbid"]].map(([v, t]) => el("option", { value: v, selected: v === (spec.leadingZeros || "keep") }, t)));
  leading.addEventListener("change", () => write({ leadingZeros: leading.value }));

  const count = ruleCount(spec);
  const details = el("details", { class: "ed-section", open: opts.open || ed.openSections.has(key) || undefined });
  details.addEventListener("toggle", () => (details.open ? ed.openSections.add(key) : ed.openSections.delete(key)));
  details.append(
    el("summary", {}, opts.title || "Validation", count ? el("span", { class: "chip" }, `${count} rule${count > 1 ? "s" : ""}`) : el("span", { class: "muted small" }, " none")),
    names.length > 1 ? el("div", { class: "muted small" }, `Applies to each of the ${names.length} fields${mixed ? " (they had different rules; editing sets the same rule on all)" : ""}.`) : null,
    checkbox("Required", spec.required, (v) => write({ required: v })),
    field("Length", lengthRow),
    checkbox("Allow gaps", spec.allowGaps !== false, (v) => write({ allowGaps: v })),
    checkbox("Allow empty ends", spec.allowEmptyEnds !== false, (v) => write({ allowEmptyEnds: v })),
    field("Leading zeros", leading),
    field("Pattern", patternControl(spec.pattern, (v) => write({ pattern: v || undefined }))),
    field("Allowed values", el("div", { class: "stack" }, allowedList, addAllowed)),
    field("Range", el("div", { class: "row gap" }, numberBox(rLow, (v) => write({ range: boundsValue(v, rHigh) }), "min"), numberBox(rHigh, (v) => write({ range: boundsValue(rLow, v) }), "max"))),
    field("On fail", onFail),
    count ? el("button", { class: "small ghost", onclick: () => ed.edit(() => { for (const n of names) delete doc.validate?.[n]; if (doc.validate && !Object.keys(doc.validate).length) delete doc.validate; }) }, "Remove all rules") : null
  );
  return details;
}

// ------------------------------------------------------------------ checks
// Names a check can compare: groups, bubble fields, zones, other check outputs
export function checkableNames(ed, exceptCheck) {
  const doc = ed.doc;
  const groups = Object.keys(doc.customLabels || {});
  const grouped = new Set(groups.flatMap((g) => ed.groupColumns(g)));
  const out = [];
  for (const g of groups) out.push({ name: g, kind: "group" });
  for (const z of Object.keys(doc.zones || {})) out.push({ name: z, kind: doc.zones[z].type || "zone" });
  for (const c of doc.checks || []) if (c !== exceptCheck) out.push({ name: c.output || c.name, kind: "check" });
  for (const label of ed.allLabels()) out.push({ name: label, kind: grouped.has(label) ? "column" : "field" });
  const seen = new Set();
  return out.filter((x) => x.name && !seen.has(x.name) && seen.add(x.name));
}

export function renderChecks(ed) {
  const checks = ed.doc.checks || [];
  const box = el("div", { class: "ed-checks" });
  if (!checks.length) box.append(el("p", { class: "muted small" }, "No checks. A check compares several reads of the same value, e.g. a barcode and its printed number."));
  checks.forEach((check, i) => {
    box.append(
      el(
        "div",
        { class: "ed-group" },
        el("div", { class: "row gap" }, el("strong", { class: "mono" }, check.name || "(unnamed)"), el("span", { class: "muted small" }, `→ ${check.output || check.name || ""}`)),
        el("div", { class: "muted small" }, `${(check.priority || check.sources || []).join(" › ")} · ${NORMALIZE_CHOICES.find(([k]) => k === normalizeKey(check.normalize))?.[1] || "custom"} · ${check.onConflict || "prefer"}`),
        el(
          "div",
          { class: "row gap" },
          el("button", { class: "small", onclick: () => openCheckDialog(ed, i) }, "Edit"),
          el("button", { class: "small danger", onclick: () => { if (confirm(`Delete check '${check.name}'?`)) ed.edit(() => { ed.doc.checks.splice(i, 1); if (!ed.doc.checks.length) delete ed.doc.checks; }); } }, "Delete")
        )
      )
    );
  });
  box.append(el("button", { class: "small", onclick: () => openCheckDialog(ed, null) }, "+ Add check"));
  return box;
}

const conflictKey = (c) => (c.onConflict === "error" ? "error" : c.onConflict === "review" || c.reviewOnConflict !== false ? "review" : "prefer");

export function openCheckDialog(ed, index) {
  const original = index === null ? null : ed.doc.checks[index];
  const draft = JSON.parse(JSON.stringify(original || { name: "", sources: [] }));
  let order = [...(draft.priority || draft.sources || [])];
  for (const s of draft.sources || []) if (!order.includes(s)) order.push(s);
  const all = checkableNames(ed, original);
  const body = el("div", { class: "ed-side ed-dialog" });
  const nameInput = el("input", { value: draft.name, placeholder: "e.g. answer_book" });
  const outputInput = el("input", { value: draft.output || "", placeholder: "same as the check name" });
  const filter = el("input", { placeholder: "filter…", class: "small" });
  const picker = el("div", { class: "formats ed-pick" });
  const chosen = el("ol", { class: "ed-drag-list" });
  const renderPicker = () => {
    picker.innerHTML = "";
    const q = filter.value.trim().toLowerCase();
    for (const item of all) {
      if (q && !item.name.toLowerCase().includes(q)) continue;
      const cb = el("input", { type: "checkbox", checked: order.includes(item.name) });
      cb.addEventListener("change", () => {
        if (cb.checked) order.push(item.name);
        else order = order.filter((n) => n !== item.name);
        renderChosen();
      });
      picker.append(el("label", {}, cb, " ", item.name, el("span", { class: "muted small" }, ` ${item.kind}`)));
    }
  };
  let dragFrom = null;
  const move = (from, to) => {
    if (to < 0 || to >= order.length || from === to) return;
    const [x] = order.splice(from, 1);
    order.splice(to, 0, x);
    renderChosen();
  };
  const renderChosen = () => {
    chosen.innerHTML = "";
    if (!order.length) chosen.append(el("li", { class: "muted small" }, "Tick the fields to compare above"));
    order.forEach((name, i) => {
      const li = el("li", { draggable: "true" }, el("span", { class: "grip" }, "⋮⋮"), el("span", { class: "mono" }, `${i + 1}. ${name}`), el("span", { class: "spacer" }),
        el("button", { class: "small ghost", onclick: () => move(i, i - 1) }, "↑"),
        el("button", { class: "small ghost", onclick: () => move(i, i + 1) }, "↓"));
      li.addEventListener("dragstart", (e) => { dragFrom = i; try { e.dataTransfer.setData("text/plain", name); } catch (err) { /* ignore */ } });
      li.addEventListener("dragover", (e) => e.preventDefault());
      li.addEventListener("drop", (e) => { e.preventDefault(); if (dragFrom !== null) move(dragFrom, i); dragFrom = null; });
      chosen.append(li);
    });
  };
  filter.addEventListener("input", renderPicker);
  let normalize = draft.normalize;
  const conflict = el("select", {}, [["prefer", "use the preferred field"], ["review", "use the preferred field and send to review"], ["error", "mark as error (blank, send to review)"]].map(([v, t]) => el("option", { value: v, selected: v === conflictKey(draft) }, t)));
  const missing = el("select", {}, [["fallback", "use the next field"], ["review", "use the next field and send to review"]].map(([v, t]) => el("option", { value: v, selected: v === (draft.onMissing || "fallback") }, t)));
  const cb = (checked) => el("input", { type: "checkbox", checked: !!checked });
  const reviewFallback = cb(draft.reviewOnFallback);
  const skipInvalid = cb(draft.skipInvalid !== false);
  const skipFlagged = cb(draft.skipFlagged);
  const row = (label, control) => help(el("label", { class: "field" }, label, control), label, "check");
  const tick = (box, label) => help(el("label", { class: "field inline-check" }, box, ` ${label}`), label, "check");
  body.append(
    row("Check name", nameInput),
    row("Fields to compare", el("div", { class: "stack" }, filter, picker)),
    row("Which field wins when they differ", chosen),
    row("Clean-up before comparing", normalizeDropdown(normalize, (v) => (normalize = v))),
    row("When values disagree", conflict),
    row("When the preferred field is missing", missing),
    tick(reviewFallback, "Also send to review when the fallback is used"),
    tick(skipInvalid, "Ignore fields that failed their own validation"),
    tick(skipFlagged, "Ignore fields already flagged for review"),
    row("Output column name", outputInput)
  );
  renderPicker();
  renderChosen();
  const save = () => {
    const name = nameInput.value.trim();
    if (!name) return toast("Give the check a name", "error");
    if ((ed.doc.checks || []).some((c, i) => i !== index && c.name === name)) return toast(`A check named '${name}' exists`, "error");
    if (!order.length) return toast("Pick at least one field to compare", "error");
    const output = outputInput.value.trim();
    const spec = { ...draft, name, sources: [...order] };
    delete spec.priority; // sources are listed in priority order
    if (normalize === undefined) delete spec.normalize;
    else spec.normalize = normalize;
    const c = conflict.value;
    spec.onConflict = c === "error" ? "error" : c === "review" ? "review" : "prefer";
    if (c === "prefer") spec.reviewOnConflict = false;
    else delete spec.reviewOnConflict;
    if (spec.onConflict === "prefer" && spec.reviewOnConflict === undefined) delete spec.onConflict;
    spec.onMissing = missing.value;
    if (spec.onMissing === "fallback") delete spec.onMissing;
    reviewFallback.checked ? (spec.reviewOnFallback = true) : delete spec.reviewOnFallback;
    skipInvalid.checked ? delete spec.skipInvalid : (spec.skipInvalid = false);
    skipFlagged.checked ? (spec.skipFlagged = true) : delete spec.skipFlagged;
    if (output && output !== name) spec.output = output;
    else delete spec.output;
    ed.edit(() => {
      ed.doc.checks = ed.doc.checks || [];
      if (index === null) ed.doc.checks.push(spec);
      else ed.doc.checks[index] = spec;
    });
    dialog.close();
  };
  const dialog = modal(original ? `Check ${original.name}` : "New cross-field check", body, [
    el("button", { class: "ghost", onclick: () => dialog.close() }, "Cancel"),
    el("button", { class: "primary", onclick: save }, "Save check"),
  ]);
}
