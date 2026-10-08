// Node test of the editor's group cascades (run by src/tests/test_editor_js.py)
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const root = process.argv[2];
const g = await import(pathToFileURL(`${root}/editor_groups.js`).href);

// expand / compress round trip
assert.deepEqual(g.expand(["roll1..3", "x"]), ["roll1", "roll2", "roll3", "x"]);
assert.deepEqual(g.compress(["roll1", "roll2", "roll3", "x", "q7"]), ["roll1..3", "x", "q7"]);

const doc = () => ({
  fieldBlocks: { Roll: { fieldLabels: ["roll1..5"] } },
  zones: { bc: { type: "barcode", options: { fallbackZone: "sr" } }, sr: { type: "ocr", options: {} } },
  customLabels: { Roll: ["roll1..5"], Other: ["q1", "q2"] },
  groupOptions: { Roll: { empty: " " } },
  validate: { Roll: { length: 5 }, roll2: { required: true }, sr: { pattern: "[0-9]+" } },
  outputColumns: ["Roll", "sr", "roll1..5"],
  checks: [{ name: "c", sources: ["Roll", "sr"], priority: ["sr", "Roll"], output: "Roll" }],
});

// Column rename roll1..5 -> rollno1..5 reaches groups, validation and outputs
let d = doc();
const map = Object.fromEntries([1, 2, 3, 4, 5].map((i) => [`roll${i}`, `rollno${i}`]));
g.cascadeRename(d, map);
assert.deepEqual(d.customLabels.Roll, ["rollno1..5"]);
assert.deepEqual(Object.keys(d.validate).sort(), ["Roll", "rollno2", "sr"]);
assert.deepEqual(d.outputColumns, ["Roll", "sr", "rollno1..5"]);

// Group and zone renames reach options, validation, checks and fallbacks
d = doc();
g.cascadeRename(d, { Roll: "rollno", sr: "serial" });
assert.deepEqual(Object.keys(d.customLabels), ["rollno", "Other"]);
assert.ok(d.groupOptions.rollno && !d.groupOptions.Roll);
assert.ok(d.validate.rollno && d.validate.serial);
assert.deepEqual(d.checks[0].sources, ["rollno", "serial"]);
assert.deepEqual(d.checks[0].priority, ["serial", "rollno"]);
assert.equal(d.checks[0].output, "rollno");
assert.equal(d.zones.bc.options.fallbackZone, "serial");

// Shrinking: drop columns from the group, or delete the group
d = doc();
g.cascadeRemove(d, ["roll4", "roll5"], "drop");
assert.deepEqual(d.customLabels.Roll, ["roll1..3"]);
assert.deepEqual(d.outputColumns, ["Roll", "sr", "roll1..3"]);
d = doc();
g.cascadeRemove(d, ["roll4", "roll5"], "delete");
assert.equal(d.customLabels.Roll, undefined);
assert.equal(d.groupOptions, undefined);
assert.equal(d.validate.Roll, undefined);
assert.deepEqual(d.checks[0].sources, ["sr"]);

// Deleting a zone removes it from checks and fallbacks
d = doc();
g.cascadeRemove(d, ["sr"]);
assert.equal(d.zones.bc.options.fallbackZone, undefined);
assert.equal(d.validate.sr, undefined);
assert.deepEqual(d.checks[0].sources, ["Roll"]);

// Broken groups and the Fix action
d = doc();
const known = new Set(["roll1", "roll2", "roll3", "q1", "q2"]);
assert.deepEqual(g.brokenGroups(d, known), [{ name: "Roll", missing: ["roll4", "roll5"] }]);
g.fixGroup(d, "Roll", known);
assert.deepEqual(d.customLabels.Roll, ["roll1..3"]);

// Block grouping detection and creation with suggested validation
d = { fieldBlocks: {}, zones: {} };
g.createGroup(d, "rollno", ["r1", "r2", "r3"]);
assert.deepEqual(d.customLabels.rollno, ["r1..3"]);
assert.deepEqual(d.groupOptions.rollno, { empty: " ", multi: "*", issue: "-" });
assert.deepEqual(d.validate.rollno, { required: true, allowGaps: false, length: 3 });
assert.equal(g.blockGroup(d, ["r1", "r2", "r3"]), "rollno");
assert.equal(g.blockGroup(d, ["r1", "r2"]), null);
console.log("editor groups ok");
