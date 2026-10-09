// Template editor: "Columns…" dialog from the top bar. Every output column
// (groups, single bubble fields, zones, check outputs) in export order: tick
// several to move them up or down together, combine them into one grouped
// field, or split a group back into its columns.
import { el, modal, toast } from "./api.js";
import { compress, createGroup, deleteGroup, groupColumns, groupNames, nameTaken } from "./editor_groups.js";
import { allOutputColumns } from "./editor_options.js";

// What a bubble column holds, from its bubble values
function valueType(values) {
  const list = (values || []).map(String);
  if (!list.length) return "text";
  if (list.every((v) => /^\d+$/.test(v))) return "digits";
  if (list.every((v) => /^[A-Za-z]+$/.test(v))) return "letters";
  return "text";
}

const TYPE_NAMES = { digits: "digits", letters: "letters", text: "text", mixed: "text (mixed)" };

function joinedType(types) {
  const set = new Set(types);
  if (set.size === 1) return [...set][0];
  return "mixed";
}

export function openColumnsDialog(ed) {
  const doc = ed.doc;
  const selected = new Set();
  const list = el("ol", { class: "ed-col-list" });
  const note = el("div", { class: "muted small" });

  const columnTypes = () => {
    const types = new Map();
    for (const name of Object.keys(doc.fieldBlocks || {})) {
      const info = ed.blockInfo(name);
      for (const label of info.labels) types.set(label, valueType(info.values));
    }
    return types;
  };

  // The export order shown: the custom order when there is one, else by name
  const order = () => {
    const all = allOutputColumns(ed);
    if (!(Array.isArray(doc.outputColumns) && doc.outputColumns.length)) return all;
    const chosen = ed.expandLabels(doc.outputColumns).filter((c) => all.includes(c));
    return [...chosen, ...all.filter((c) => !chosen.includes(c))];
  };

  const describe = (name, types) => {
    const groups = new Set(groupNames(doc));
    if (groups.has(name)) {
      const cols = groupColumns(doc, name);
      const type = joinedType(cols.map((c) => types.get(c) || "text"));
      return { kind: "group", columns: cols, text: `group · ${cols.length} col · ${TYPE_NAMES[type]}`, type };
    }
    if (types.has(name)) return { kind: "field", columns: [name], text: `bubble field · ${types.get(name)}`, type: types.get(name) };
    if (doc.zones && doc.zones[name]) return { kind: "zone", columns: [], text: `${doc.zones[name].type || "zone"} zone`, type: "text" };
    return { kind: "check", columns: [], text: "check output", type: "text" };
  };

  const refresh = () => {
    const types = columnTypes();
    const names = order();
    for (const name of [...selected]) if (!names.includes(name)) selected.delete(name);
    list.innerHTML = "";
    names.forEach((name) => {
      const info = describe(name, types);
      const box = el("input", { type: "checkbox", checked: selected.has(name) });
      box.addEventListener("change", () => {
        if (box.checked) selected.add(name);
        else selected.delete(name);
        refresh();
      });
      list.append(el("li", { class: selected.has(name) ? "on" : "", onclick: (e) => { if (e.target !== box) box.click(); } }, box, el("span", { class: "mono" }, name), el("span", { class: "muted small" }, info.text)));
    });
    const picked = names.filter((n) => selected.has(n)).map((n) => ({ name: n, ...describe(n, types) }));
    const blocked = picked.filter((p) => p.kind === "zone" || p.kind === "check");
    const type = joinedType(picked.flatMap((p) => p.columns.map((c) => types.get(c) || "text")));
    if (!picked.length) note.textContent = "Tick columns to move, combine or split them.";
    else if (blocked.length) note.textContent = `${blocked.map((p) => p.name).join(", ")}: zones and check outputs can be moved but not combined.`;
    else if (picked.length > 1) note.textContent = `Combining gives ${picked.reduce((n, p) => n + p.columns.length, 0)} columns${type === "mixed" ? ": the values differ in type (digits and letters), so the joined field is text" : ` of ${TYPE_NAMES[type]}`}.`;
    else note.textContent = "";
    combineBtn.disabled = picked.length < 2 || blocked.length > 0;
    splitBtn.disabled = !picked.some((p) => p.kind === "group");
    upBtn.disabled = downBtn.disabled = !picked.length;
  };

  // Moving sets a custom export order (the Output columns list follows it)
  const move = (step) => {
    const names = order();
    const idx = names.map((n, i) => (selected.has(n) ? i : -1)).filter((i) => i >= 0);
    if (!idx.length) return;
    if ((step < 0 && idx[0] === 0) || (step > 0 && idx[idx.length - 1] === names.length - 1)) return;
    const next = names.slice();
    const walk = step < 0 ? idx : idx.slice().reverse();
    for (const i of walk) {
      const [x] = next.splice(i, 1);
      next.splice(i + step, 0, x);
    }
    ed.edit(() => (doc.outputColumns = ed.compressLabels(next)));
    refresh();
  };

  const combine = () => {
    const types = columnTypes();
    const names = order();
    const picked = names.filter((n) => selected.has(n)).map((n) => ({ name: n, ...describe(n, types) }));
    if (picked.length < 2) return;
    const columns = picked.flatMap((p) => p.columns);
    const firstGroup = picked.find((p) => p.kind === "group");
    const nameInput = el("input", { value: firstGroup ? firstGroup.name : "", placeholder: "e.g. rollno" });
    const type = joinedType(columns.map((c) => types.get(c) || "text"));
    const ask = modal(
      "Combine into one field",
      el(
        "div",
        { class: "ed-side" },
        el("p", { class: "muted small" }, `${columns.length} columns, joined in this order: ${compress(columns).join(", ")}.`),
        type === "mixed" ? el("p", { class: "small" }, "The columns hold different kinds of values (digits and letters); the joined field is read as text.") : null,
        picked.some((p) => p.kind === "group") ? el("p", { class: "muted small" }, `The groups ${picked.filter((p) => p.kind === "group").map((p) => p.name).join(", ")} are replaced by the new one.`) : null,
        el("label", { class: "field" }, "Name of the joined field", nameInput)
      ),
      [
        el("button", { class: "ghost", onclick: () => ask.close() }, "Cancel"),
        el(
          "button",
          {
            class: "primary",
            onclick: () => {
              const name = nameInput.value.trim();
              if (!name) return toast("Give the field a name", "error");
              const replaced = new Set(picked.filter((p) => p.kind === "group").map((p) => p.name));
              if (!replaced.has(name) && nameTaken(ed, name)) return toast(`'${name}' is already used by a field, zone, group or check`, "error");
              // The new field takes the place of the first column picked
              const position = names.indexOf(picked[0].name);
              const custom = Array.isArray(doc.outputColumns) && doc.outputColumns.length > 0;
              const pickedNames = new Set(picked.map((p) => p.name));
              ed.edit(() => {
                for (const group of replaced) deleteGroup(doc, group);
                createGroup(doc, name, columns, { validate: true });
                if (custom) {
                  const next = names.filter((n) => !pickedNames.has(n));
                  next.splice(Math.min(position, next.length), 0, name);
                  doc.outputColumns = ed.compressLabels(next);
                }
              });
              selected.clear();
              selected.add(name);
              ask.close();
              toast(`'${name}' now joins ${columns.length} columns`, "ok");
              refresh();
            },
          },
          "Combine"
        ),
      ]
    );
    setTimeout(() => nameInput.focus(), 0);
  };

  const split = () => {
    const groups = order().filter((n) => selected.has(n) && groupNames(doc).includes(n));
    if (!groups.length) return;
    const custom = Array.isArray(doc.outputColumns) && doc.outputColumns.length > 0;
    const names = order();
    ed.edit(() => {
      const next = [];
      for (const n of names) {
        if (groups.includes(n)) next.push(...groupColumns(doc, n));
        else next.push(n);
      }
      for (const group of groups) deleteGroup(doc, group);
      if (custom) doc.outputColumns = ed.compressLabels(next);
    });
    selected.clear();
    toast(`Split ${groups.join(", ")} back into separate columns`, "ok");
    refresh();
  };

  const upBtn = el("button", { class: "small", title: "Move the ticked columns up", onclick: () => move(-1) }, "↑ Up");
  const downBtn = el("button", { class: "small", title: "Move the ticked columns down", onclick: () => move(1) }, "↓ Down");
  const combineBtn = el("button", { class: "small primary", title: "Join the ticked columns into one field", onclick: combine }, "Combine…");
  const splitBtn = el("button", { class: "small", title: "Turn the ticked groups back into separate columns", onclick: split }, "Split");
  const all = el("button", { class: "small ghost", onclick: () => { order().forEach((n) => selected.add(n)); refresh(); } }, "All");
  const none = el("button", { class: "small ghost", onclick: () => { selected.clear(); refresh(); } }, "None");
  refresh();
  modal(
    "Columns and groups",
    el(
      "div",
      {},
      el("p", { class: "muted small" }, "Every column the template exports, in export order. Moving a column sets a custom order."),
      el("div", { class: "row gap wrap ed-col-tools" }, all, none, el("span", { class: "sep" }), upBtn, downBtn, combineBtn, splitBtn),
      note,
      list
    ),
    [],
    { wide: true }
  );
}
