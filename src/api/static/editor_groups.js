// Template editor: grouped fields (customLabels + groupOptions), name cascades
// (renames and deletes reach every group, validation rule, check and output
// column in the same undo step) and broken-group repair.
import { add, el, modal, toast } from "./api.js";
import { renderValidation } from "./editor_checks.js";
import { help } from "./editor_help.js";

export const GROUP_DEFAULTS = { empty: " ", multi: "*", issue: "-" };

const RANGE = /^([^.\d]+)(\d+)\.{2,3}(\d+)$/;

export function expand(list) {
  const out = [];
  for (const s of list || []) {
    const m = RANGE.exec(String(s));
    if (!m) {
      out.push(String(s));
      continue;
    }
    for (let i = Number(m[2]); i <= Number(m[3]); i++) out.push(`${m[1]}${i}`);
  }
  return out;
}

// Columns back to field strings: runs like roll1, roll2, roll3 become roll1..3
export function compress(labels) {
  const out = [];
  let i = 0;
  while (i < labels.length) {
    const m = /^([^.\d]+)(\d+)$/.exec(labels[i]);
    if (!m) {
      out.push(labels[i++]);
      continue;
    }
    let j = i;
    while (j + 1 < labels.length) {
      const n = /^([^.\d]+)(\d+)$/.exec(labels[j + 1]);
      if (!n || n[1] !== m[1] || Number(n[2]) !== Number(m[2]) + (j + 1 - i) || n[2].startsWith("0")) break;
      j++;
    }
    if (j > i && !m[2].startsWith("0")) out.push(`${m[1]}${m[2]}..${Number(m[2]) + (j - i)}`);
    else for (let k = i; k <= j; k++) out.push(labels[k]);
    i = j + 1;
  }
  return out;
}

export const groupNames = (doc) => Object.keys(doc.customLabels || {});
export const groupColumns = (doc, name) => expand((doc.customLabels || {})[name]);

export function groupsUsing(doc, labels) {
  const set = new Set(labels);
  return groupNames(doc)
    .map((name) => ({ name, used: groupColumns(doc, name).filter((c) => set.has(c)) }))
    .filter((g) => g.used.length);
}

// The group whose columns are exactly this block's columns, if any
export function blockGroup(doc, labels) {
  const key = labels.join("\u0001");
  return groupNames(doc).find((name) => groupColumns(doc, name).join("\u0001") === key) || null;
}

export function brokenGroups(doc, known) {
  return groupNames(doc)
    .map((name) => ({ name, missing: groupColumns(doc, name).filter((c) => !known.has(c)) }))
    .filter((g) => g.missing.length);
}

function pruneEmpty(doc) {
  for (const key of ["customLabels", "groupOptions", "validate"]) {
    if (doc[key] && !Object.keys(doc[key]).length) delete doc[key];
  }
  if (Array.isArray(doc.checks) && !doc.checks.length) delete doc.checks;
  if (Array.isArray(doc.outputColumns) && !doc.outputColumns.length) delete doc.outputColumns;
}

function renameInList(list, map) {
  if (!Array.isArray(list)) return list;
  const expanded = expand(list);
  if (!expanded.some((x) => map[x] !== undefined)) return list;
  return compress(expanded.map((x) => map[x] ?? x));
}

// Rename names anywhere they are referenced: {old: new}. Covers bubble
// columns, groups and zones; the caller renames the definition itself.
export function cascadeRename(doc, map) {
  if (!Object.keys(map).length) return;
  const renameKeys = (obj) => {
    if (!obj) return obj;
    const out = {};
    for (const [k, v] of Object.entries(obj)) out[map[k] ?? k] = v;
    return out;
  };
  if (doc.customLabels) {
    const next = {};
    for (const [name, cols] of Object.entries(doc.customLabels)) next[map[name] ?? name] = renameInList(cols, map);
    doc.customLabels = next;
  }
  if (doc.groupOptions) doc.groupOptions = renameKeys(doc.groupOptions);
  if (doc.validate) doc.validate = renameKeys(doc.validate);
  if (doc.outputColumns) doc.outputColumns = renameInList(doc.outputColumns, map);
  if (doc.primaryKey) doc.primaryKey = doc.primaryKey.map((c) => map[c] ?? c);
  for (const check of doc.checks || []) {
    check.sources = (check.sources || []).map((s) => map[s] ?? s);
    if (check.priority) check.priority = check.priority.map((s) => map[s] ?? s);
    if (check.output && map[check.output] !== undefined) check.output = map[check.output];
  }
  for (const zone of Object.values(doc.zones || {})) {
    const fb = zone.options?.fallbackZone;
    if (fb && map[fb] !== undefined) zone.options.fallbackZone = map[fb];
  }
}

// Remove names (bubble columns or zones) from every reference.
// mode "drop": take the columns out of their groups; "delete": delete those groups.
export function cascadeRemove(doc, names, mode = "drop") {
  const gone = new Set(names);
  for (const g of groupsUsing(doc, names)) {
    const left = groupColumns(doc, g.name).filter((c) => !gone.has(c));
    if (mode === "delete" || !left.length) deleteGroup(doc, g.name);
    else doc.customLabels[g.name] = compress(left);
  }
  for (const n of names) if (doc.validate) delete doc.validate[n];
  if (doc.outputColumns) doc.outputColumns = compress(expand(doc.outputColumns).filter((c) => !gone.has(c)));
  removeFromChecks(doc, gone);
  for (const zone of Object.values(doc.zones || {})) {
    if (zone.options?.fallbackZone && gone.has(zone.options.fallbackZone)) delete zone.options.fallbackZone;
  }
  pruneEmpty(doc);
}

function removeFromChecks(doc, gone) {
  if (!doc.checks) return;
  doc.checks = doc.checks
    .map((c) => ({ ...c, sources: (c.sources || []).filter((s) => !gone.has(s)), ...(c.priority ? { priority: c.priority.filter((s) => !gone.has(s)) } : {}) }))
    .filter((c) => c.sources.length);
}

export function deleteGroup(doc, name) {
  if (doc.customLabels) delete doc.customLabels[name];
  if (doc.groupOptions) delete doc.groupOptions[name];
  if (doc.validate) delete doc.validate[name];
  if (doc.outputColumns) doc.outputColumns = doc.outputColumns.filter((c) => c !== name);
  setPrimaryKey(doc, name, false);
  removeFromChecks(doc, new Set([name]));
  pruneEmpty(doc);
}

// Primary key: output columns that identify a sheet; sheets of a job with
// the same key are listed as duplicates on the Results screen
export function setPrimaryKey(doc, name, on) {
  const keys = (doc.primaryKey || []).filter((c) => c !== name);
  if (on) keys.push(name);
  if (keys.length) doc.primaryKey = keys;
  else delete doc.primaryKey;
}

export function primaryKeyTick(ed, name) {
  const tick = el("input", { type: "checkbox", checked: (ed.doc.primaryKey || []).includes(name) });
  tick.addEventListener("change", () => ed.edit(() => setPrimaryKey(ed.doc, name, tick.checked)));
  return el(
    "label",
    { class: "field inline-check", title: "Sheets of a job with the same value here (and in any other key column) are flagged as duplicates" },
    tick,
    " Primary key"
  );
}

// Ask what to do with groups that use columns about to disappear.
// Resolves "drop", "delete" or null (cancel); "drop" right away when no group is affected.
export function askGroupImpact(doc, labels, action = "This change") {
  const affected = groupsUsing(doc, labels);
  if (!affected.length) return Promise.resolve("drop");
  return new Promise((resolve) => {
    let answer = null;
    const lines = affected.map((g) => el("li", {}, el("strong", {}, g.name), ` uses ${g.used.join(", ")}`));
    const choose = (value) => {
      answer = value;
      dialog.close();
    };
    const dialog = modal(
      "Grouped fields use these columns",
      el("div", {}, el("p", {}, `${action} removes columns that grouped fields use:`), el("ul", {}, lines), el("p", { class: "muted small" }, "Remove them from the group (the group keeps its other columns), or delete the group?")),
      [
        el("button", { class: "ghost", onclick: () => choose(null) }, "Cancel"),
        el("button", { class: "danger", onclick: () => choose("delete") }, affected.length > 1 ? "Delete the groups" : "Delete the group"),
        el("button", { class: "primary", onclick: () => choose("drop") }, "Remove from the group"),
      ],
      { onClose: () => resolve(answer) }
    );
  });
}

export function nameTaken(ed, name, except) {
  if (!name || name === except) return false;
  return !!(ed.doc.customLabels?.[name] || ed.doc.zones?.[name] || ed.allLabels().has(name) || (ed.doc.checks || []).some((c) => (c.output || c.name) === name));
}

// ------------------------------------------------------------------ GUI
const PLACEHOLDERS = [
  [" ", "space"],
  ["_", "_"],
  ["-", "-"],
  ["*", "*"],
  ["?", "?"],
  ["0", "0"],
  ["", "nothing"],
];

function charPicker(value, onchange, { allowSkip = false } = {}) {
  const known = PLACEHOLDERS.some(([v]) => v === value);
  const options = [...(allowSkip ? [["__skip", "skip the column (join the next one)"]] : []), ...PLACEHOLDERS.map(([v, t]) => [v, `keep ${t === "nothing" ? "nothing" : `"${t}"`}`]), ["__other", "other character…"]];
  const current = value === null ? "__skip" : known ? value : "__other";
  const sel = el("select", {}, options.map(([v, t]) => el("option", { value: v, selected: v === current }, t)));
  const other = el("input", { value: known || value === null ? "" : value, maxlength: "3", size: "3", class: current === "__other" ? "" : "hidden", placeholder: "char" });
  sel.addEventListener("change", () => {
    if (sel.value === "__other") {
      other.classList.remove("hidden");
      other.focus();
      return;
    }
    onchange(sel.value === "__skip" ? null : sel.value);
  });
  other.addEventListener("change", () => onchange(other.value));
  return el("span", { class: "row gap" }, sel, other);
}

// Placeholders of one group (what an empty / multi-marked / flagged column becomes)
export function renderGroupOptions(ed, name) {
  const doc = ed.doc;
  const opts = doc.groupOptions?.[name];
  const box = el("div", { class: "ed-group-opts" });
  if (!opts) {
    add(box, 
      el("p", { class: "muted small" }, "Plain join (templates made before placeholders): an empty column vanishes and a multi-marked column puts all its values in, so later digits shift."),
      el("button", { class: "small", onclick: () => ed.edit(() => ((doc.groupOptions = doc.groupOptions || {})[name] = { ...GROUP_DEFAULTS })) }, "Use one character per column")
    );
    return box;
  }
  const set = (key, value) =>
    ed.edit(() => {
      doc.groupOptions[name] = { ...doc.groupOptions[name], [key]: value };
    });
  const row = (label, key, control) => help(el("label", { class: "field" }, label, control), key === "empty" ? "groupEmpty" : key === "multi" ? "groupMulti" : "groupIssue");
  add(box, 
    row("Empty column (no bubble marked)", "empty", charPicker(opts.empty === undefined ? " " : opts.empty, (v) => set("empty", v), { allowSkip: true })),
    row("Multi-marked column", "multi", charPicker(opts.multi ?? "*", (v) => set("multi", v))),
    row("Column with an issue", "issue", charPicker(opts.issue ?? "-", (v) => set("issue", v))),
    el("div", { class: "muted small" }, `Example: ${exampleValue(opts)} (a 6-digit number with one empty, one multi-marked and one doubtful column). Flagged groups go to review and are highlighted in Results.`),
    el("button", { class: "small ghost", onclick: () => ed.edit(() => { delete doc.groupOptions[name]; pruneEmpty(doc); }) }, "Back to plain join")
  );
  return box;
}

function exampleValue(opts) {
  const empty = opts.empty === null ? "" : opts.empty ?? " ";
  return `"12${empty}4${opts.multi ?? "*"}${opts.issue ?? "-"}"`.replace(/ /g, "␣");
}

// Validation offered when a group is created: required, length = columns
// (with "skip" for empty columns a shorter value then fails the length check)
export function suggestedValidation(columns) {
  return { required: true, allowGaps: false, length: columns };
}

export function createGroup(doc, name, columns, { validate = true } = {}) {
  doc.customLabels = doc.customLabels || {};
  doc.customLabels[name] = compress(columns);
  doc.groupOptions = doc.groupOptions || {};
  doc.groupOptions[name] = { ...GROUP_DEFAULTS };
  if (validate) {
    doc.validate = doc.validate || {};
    doc.validate[name] = { ...suggestedValidation(columns.length), ...(doc.validate[name] || {}) };
  }
}

// Dialog to create or edit a group: name, columns in order (drag to reorder)
export function openGroupDialog(ed, { name = "", columns = [], existing = null } = {}) {
  const doc = ed.doc;
  let order = [...columns];
  const nameInput = el("input", { value: name, placeholder: "e.g. rollno" });
  const list = el("ol", { class: "ed-drag-list" });
  const offerValidation = el("input", { type: "checkbox", checked: !existing });
  const otherGroups = new Set(groupNames(doc).filter((g) => g !== existing).flatMap((g) => groupColumns(doc, g)));
  const addSel = el("select", { class: "small" });
  const refresh = () => {
    list.innerHTML = "";
    order.forEach((col, i) => list.append(dragItem(col, i)));
    addSel.innerHTML = "";
    addSel.append(el("option", { value: "" }, "+ add a column…"));
    for (const label of ed.allLabels()) if (!order.includes(label) && !otherGroups.has(label)) addSel.append(el("option", { value: label }, label));
    count.textContent = `${order.length} column(s), joined top to bottom`;
  };
  const count = el("div", { class: "muted small" });
  let dragFrom = null;
  const dragItem = (col, i) => {
    const li = el("li", { draggable: "true", title: "Drag to reorder" }, el("span", { class: "grip" }, "⋮⋮"), el("span", { class: "mono" }, col), el("span", { class: "spacer" }),
      el("button", { class: "small ghost", title: "Move up", onclick: () => move(i, i - 1) }, "↑"),
      el("button", { class: "small ghost", title: "Move down", onclick: () => move(i, i + 1) }, "↓"),
      el("button", { class: "small ghost", title: "Remove from the group", onclick: () => { order.splice(i, 1); refresh(); } }, "✕"));
    li.addEventListener("dragstart", (e) => { dragFrom = i; e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", col); } catch (err) { /* old browsers */ } });
    li.addEventListener("dragover", (e) => { e.preventDefault(); li.classList.add("over"); });
    li.addEventListener("dragleave", () => li.classList.remove("over"));
    li.addEventListener("drop", (e) => { e.preventDefault(); if (dragFrom !== null) move(dragFrom, i); dragFrom = null; });
    return li;
  };
  const move = (from, to) => {
    if (to < 0 || to >= order.length || from === to) return;
    const [x] = order.splice(from, 1);
    order.splice(to, 0, x);
    refresh();
  };
  addSel.addEventListener("change", () => {
    if (addSel.value) order.push(addSel.value);
    refresh();
  });
  refresh();
  const body = el(
    "div",
    { class: "ed-side" },
    help(el("label", { class: "field" }, "Name of the joined field (output column)", nameInput), "groupName"),
    count,
    list,
    addSel,
    existing ? null : el("label", { class: "small" }, offerValidation, ` Also check it: required, no gaps, length = number of columns`)
  );
  const save = () => {
    const newName = nameInput.value.trim();
    if (!newName) return toast("Give the group a name", "error");
    if (nameTaken(ed, newName, existing)) return toast(`'${newName}' is already used by a field, zone, group or check`, "error");
    if (!order.length) return toast("A group needs at least one column", "error");
    ed.edit(() => {
      if (existing) {
        if (newName !== existing) cascadeRename(doc, { [existing]: newName });
        doc.customLabels[newName] = compress(order);
      } else {
        createGroup(doc, newName, order, { validate: offerValidation.checked });
      }
      ed.multi = [];
    });
    dialog.close();
    toast(existing ? `Group '${newName}' updated` : `Group '${newName}' created: ${order.length} columns, one character each`, "ok");
  };
  const dialog = modal(existing ? `Edit group ${existing}` : "Group as one field", body, [
    el("button", { class: "ghost", onclick: () => dialog.close() }, "Cancel"),
    el("button", { class: "primary", onclick: save }, existing ? "Save" : "Create group"),
  ]);
  setTimeout(() => nameInput.focus(), 0);
}

export function renameGroup(ed, name) {
  const next = (prompt(`New name for group '${name}':`, name) || "").trim();
  if (!next || next === name) return;
  if (nameTaken(ed, next, name)) return toast(`'${next}' is already used`, "error");
  ed.edit(() => cascadeRename(ed.doc, { [name]: next }));
}

export function confirmDeleteGroup(ed, name) {
  const usedBy = (ed.doc.checks || []).filter((c) => (c.sources || []).includes(name)).map((c) => c.name);
  const extra = usedBy.length ? `\nIt is also removed from the check(s): ${usedBy.join(", ")}.` : "";
  if (!confirm(`Delete group '${name}'? Its columns stay and are exported one by one.${extra}`)) return;
  ed.edit(() => deleteGroup(ed.doc, name));
}

// "Output as one field" for a whole block
export function renderBlockGrouping(ed, o) {
  const doc = ed.doc;
  const group = blockGroup(doc, o.labels);
  const partOf = group ? [] : groupsUsing(doc, o.labels);
  const box = el("div", { class: "ed-section-body" });
  const nameInput = el("input", { value: group || suggestGroupName(ed, o), placeholder: "e.g. rollno" });
  const check = el("input", { type: "checkbox", checked: !!group, disabled: partOf.length > 0 && !group });
  check.addEventListener("change", () => {
    if (check.checked) {
      const name = nameInput.value.trim();
      if (!name) {
        check.checked = false;
        return toast("Type a name for the joined field first", "error");
      }
      if (nameTaken(ed, name)) {
        check.checked = false;
        return toast(`'${name}' is already used`, "error");
      }
      ed.edit(() => createGroup(doc, name, o.labels));
      toast(`'${name}' = ${o.labels[0]}..${o.labels[o.labels.length - 1]}; validation added (required, length ${o.labels.length})`, "ok", 5000);
    } else if (group) {
      ed.edit(() => deleteGroup(doc, group));
    }
  });
  nameInput.addEventListener("change", () => {
    const v = nameInput.value.trim();
    if (!group || !v || v === group) return;
    if (nameTaken(ed, v, group)) return toast(`'${v}' is already used`, "error");
    ed.edit(() => cascadeRename(doc, { [group]: v }));
  });
  add(box, 
    help(el("label", { class: "field inline-check" }, check, " Output as one field"), "outputAsOne"),
    help(el("label", { class: "field" }, "Name of the joined field", nameInput), "groupName")
  );
  if (group) add(box, primaryKeyTick(ed, group));
  else if (o.labels.length === 1 && !partOf.length) add(box, primaryKeyTick(ed, o.labels[0]));
  if (partOf.length) add(box, el("div", { class: "muted small" }, `Columns are already in group ${partOf.map((g) => g.name).join(", ")}: edit it on the Page panel.`));
  if (group) {
    add(box, el("h4", {}, `What each column of ${group} becomes`), renderGroupOptions(ed, group));
    add(box, renderValidation(ed, [group], { title: `Validation of ${group}`, columns: o.labels.length }));
  }
  return box;
}

function suggestGroupName(ed, o) {
  const m = /^([^.\d]+)\d+$/.exec(o.labels[0] || "");
  const base = m ? m[1] : o.name;
  return nameTaken(ed, base) ? `${base}no` : base;
}

// Side panel when several blocks are selected (Shift+click)
export function renderMultiSelection(ed) {
  const blocks = ed.multi.filter((s) => s.kind === "block" && ed.doc.fieldBlocks[s.name]);
  const columns = blocks.flatMap((s) => ed.blockInfo(s.name).labels);
  const inGroups = groupsUsing(ed.doc, columns);
  return el(
    "div",
    {},
    el("h3", {}, `${blocks.length} blocks selected`),
    el("p", { class: "muted small" }, blocks.map((b) => b.name).join(", "), ` · ${columns.length} columns. Shift+click adds or removes a block.`),
    inGroups.length ? el("p", { class: "muted small" }, `Already grouped: ${inGroups.map((g) => `${g.name} (${g.used.length})`).join(", ")}. Those columns are left out.`) : null,
    el("button", { class: "small primary", onclick: () => openGroupDialog(ed, { columns: columns.filter((c) => !inGroups.some((g) => g.used.includes(c))) }) }, "Group as one field…"),
    " ",
    el("button", { class: "small ghost", onclick: () => { ed.multi = []; ed.renderSide(); ed.draw(); } }, "Clear selection")
  );
}

// "Grouped fields" list on the Page panel, with broken groups in red
export function renderGroupList(ed) {
  const doc = ed.doc;
  const known = ed.allLabels();
  for (const z of Object.keys(doc.zones || {})) known.add(z);
  const broken = new Map(brokenGroups(doc, known).map((g) => [g.name, g.missing]));
  const names = groupNames(doc);
  const box = el("div", { class: "ed-groups" });
  if (!names.length) add(box, el("p", { class: "muted small" }, "No grouped fields. Tick \"Output as one field\" on a block, or Shift+click several blocks and choose \"Group as one field\"."));
  for (const name of names) {
    const cols = groupColumns(doc, name);
    const missing = broken.get(name);
    const opts = doc.groupOptions?.[name];
    add(box, 
      el(
        "div",
        { class: `ed-group${missing ? " broken" : ""}` },
        el("div", { class: "row gap" }, el("strong", { class: "mono" }, name), el("span", { class: "muted small" }, `${cols.length} col · ${compress(cols).join(", ")}`)),
        missing ? el("div", { class: "small err" }, `Missing column(s): ${missing.join(", ")}`) : null,
        primaryKeyTick(ed, name),
        el("div", { class: "muted small" }, opts ? `empty ${show(opts.empty === undefined ? " " : opts.empty)} · multi ${show(opts.multi ?? "*")} · issue ${show(opts.issue ?? "-")}` : "plain join"),
        el(
          "div",
          { class: "row gap" },
          missing ? el("button", { class: "small primary", title: "Drop the missing columns from the group", onclick: () => ed.edit(() => fixGroup(doc, name, known)) }, "Fix") : null,
          el("button", { class: "small", onclick: () => openGroupDetails(ed, name) }, "Edit"),
          el("button", { class: "small", onclick: () => renameGroup(ed, name) }, "Rename"),
          el("button", { class: "small danger", onclick: () => confirmDeleteGroup(ed, name) }, "Delete")
        )
      )
    );
  }
  return box;
}

const show = (v) => (v === null ? "skip" : v === " " ? "␣" : v === "" ? "nothing" : `"${v}"`);

export function fixGroup(doc, name, known) {
  const left = groupColumns(doc, name).filter((c) => known.has(c));
  if (!left.length) deleteGroup(doc, name);
  else doc.customLabels[name] = compress(left);
}

// Full editor for one group: columns, placeholders, validation
export function openGroupDetails(ed, name) {
  const render = () => {
    if (!ed.doc.customLabels?.[name]) {
      dialog.close();
      return;
    }
    body.innerHTML = "";
    add(body, 
      el("div", { class: "row gap" }, el("span", { class: "mono" }, compress(groupColumns(ed.doc, name)).join(", ")), el("button", { class: "small", onclick: () => { dialog.close(); openGroupDialog(ed, { name, columns: groupColumns(ed.doc, name), existing: name }); } }, "Columns and order…")),
      el("h4", {}, "What each column becomes"),
      renderGroupOptions(ed, name),
      renderValidation(ed, [name], { title: "Validation", columns: groupColumns(ed.doc, name).length, open: true })
    );
  };
  const body = el("div", { class: "ed-side ed-dialog" });
  const dialog = modal(`Group ${name}`, body, [el("button", { class: "primary", onclick: () => dialog.close() }, "Done")], { onClose: () => (ed.afterEditHooks = ed.afterEditHooks.filter((f) => f !== render)) });
  ed.afterEditHooks.push(render);
  render();
}

// Red banner on open when groups name columns no block defines
export function renderBrokenBanner(ed) {
  const known = ed.allLabels();
  for (const z of Object.keys(ed.doc.zones || {})) known.add(z);
  const broken = brokenGroups(ed.doc, known);
  if (!broken.length) return null;
  return el(
    "div",
    { class: "ed-broken" },
    el("strong", {}, "Grouped fields with missing columns"),
    broken.map((g) =>
      el(
        "div",
        { class: "row gap wrap" },
        el("span", {}, el("span", { class: "mono" }, g.name), `: ${g.missing.join(", ")} not defined by any block`),
        el("button", { class: "small primary", onclick: () => ed.edit(() => fixGroup(ed.doc, g.name, known)) }, "Fix (drop missing)"),
        el("button", { class: "small danger", onclick: () => ed.edit(() => deleteGroup(ed.doc, g.name)) }, "Delete group")
      )
    )
  );
}
