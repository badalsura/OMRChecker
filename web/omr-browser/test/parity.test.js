"use strict";
// Parity of the browser engine with ground truth and with the Python engine.
// Fixtures are rendered by test/make_fixtures.py (needs the repo's Python env), or
// taken from OMR_FIXTURES_DIR. Run: npm test
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const H = require("./harness.js");

const fixtures = H.ensureFixtures(Number(process.env.OMR_PARITY_N || 6));
const skip = fixtures ? false : "Python with OpenCV is not available to render fixtures";
const report = [];

async function maybeEnableBarcodes() {
  let Z;
  try {
    Z = require("zxing-wasm/reader");
  } catch (_) {
    return false;
  }
  const wasm = require.resolve("zxing-wasm/reader").replace(/dist[\\/]cjs[\\/]reader[\\/]index\.js$/, path.join("dist", "reader", "zxing_reader.wasm"));
  await H.OMR.enableBarcodes({ module: Z, wasmBinary: fs.readFileSync(wasm) });
  return true;
}

// Scenarios after the first five need fixtures from the current make_fixtures.py.
// The built-in barcode scenarios run before "zones" switches zxing-wasm on (their
// zones also name engines: ["builtin"], as the Python fixtures do).
const SCENARIOS = ["clean", "scan", "phone", "croppage", "fixed", "rectify", "color_red", "color_lab", "rules", "barcodes", "zones"];
// Every output of these must match Python exactly: zones (value, flags, engine),
// checks, validation, the response row and the whole review list
const EXACT = ["fixed", "rectify", "color_red", "color_lab", "rules", "barcodes"];

// Before any test switches zxing-wasm on: the default engine chain falls back to the
// built-in decoder and flags the read like Python's barcode.py does
test("barcode zones without zxing-wasm use the built-in decoder", { skip }, async (t) => {
  const file = path.join(fixtures, "units.json");
  if (!fs.existsSync(file)) return t.skip("no units.json (regenerate fixtures)");
  if (H.OMR.barcodesAvailable()) return t.skip("zxing-wasm is already enabled");
  const code = JSON.parse(fs.readFileSync(file, "utf8")).linear.find((c) => c.format === "Code128" && c.python && c.height < c.width);
  const w = Math.max(code.width, 120), h = code.height + 60;
  const page = new Uint8Array(w * h).fill(255);
  const bars = Buffer.from(code.gray, "base64");
  for (let y = 0; y < code.height; y++) page.set(bars.subarray(y * code.width, (y + 1) * code.width), y * w);
  const engine = await H.OMR.loadTemplate({
    pageDimensions: [w, h], bubbleDimensions: [12, 12],
    fieldBlocks: { A: { fieldType: "QTYPE_MCQ4", origin: [10, code.height + 20], bubblesGap: 20, labelsGap: 20, fieldLabels: ["q1"] } },
    zones: { id: { type: "barcode", origin: [10, 10], dimensions: [code.width - 20, code.height - 20], options: { formats: ["Code128"] } } },
  });
  const r = await engine.scan({ width: w, height: h, data: page });
  assert.equal(r.zones.id.value, code.python.text);
  assert.equal(r.zones.id.engine, "builtin");
  assert.deepEqual(r.zones.id.flags, ["decoded_by_fallback"]);
  assert.deepEqual(r.zones.id.details.engines, ["builtin"]);
  assert.equal(r.zones.id.needs_review, false);
});

for (const name of SCENARIOS) {
  test(`parity: ${name}`, { skip }, async (t) => {
    const dir = path.join(fixtures, name);
    if (!fs.existsSync(path.join(dir, "expected.json"))) return t.skip(`no '${name}' fixtures (regenerate with make_fixtures.py)`);
    if (name === "zones" && !(await maybeEnableBarcodes())) return t.skip("zxing-wasm not installed (npm install)");
    const run = await H.runScenario(dir);
    const st = H.compare(run);
    report.push(H.summary(name, st));
    assert.equal(st.jsErrors, st.pyErrors, "registration failures differ from Python");
    assert.equal(st.jsErrors, 0, "synthetic sheets must register");
    assert.deepEqual(st.truthErrors, [], "an unflagged field disagrees with the ground truth");
    assert.ok(st.valueAgreement >= 0.995, `value agreement ${st.valueAgreement}`);
    assert.ok(st.reviewAgreement >= 0.98, `needs_review agreement ${st.reviewAgreement}`);
    if (name === "phone") {
      const flipped = run.sheets.filter((s) => s.js.registration && s.js.registration.orientation === 180);
      assert.ok(flipped.length >= 1, "the 180-degree sheets were detected as rotated");
    }
    if (EXACT.includes(name)) {
      assert.deepEqual(st.zoneDisagreements, [], "zone results differ from Python");
      assert.deepEqual(st.rulesDisagreements.map((d) => d.file), [], "checks / validation / responses differ from Python");
      assert.deepEqual(st.reviewListDisagreements.map((d) => d.file), [], "review lists differ from Python");
      assert.equal(st.valueAgreement, 1);
      assert.equal(st.reviewAgreement, 1);
      assert.equal(st.statusAgree, st.sheets);
    }
    if (name === "fixed") for (const s of run.sheets) assert.equal(s.js.thresholds.mode, "fixed");
    if (name === "rectify" && st.sheets >= 4) {
      const failed = run.sheets.filter((s) => Object.values(s.js.fields).some((f) => f.flags.includes("rectify_failed")));
      assert.ok(failed.length >= 1, "a borderless sheet was flagged rectify_failed");
    }
    if (name === "color_red" && st.sheets >= 4) assert.ok(st.sheetItems >= 1, "a too_few_marks sheet item was raised");
    if (name === "rules") {
      const fallbacks = run.sheets.filter((s) => s.js.checks.sheet_id && s.js.checks.sheet_id.chosen_source === "sheet_id_copy");
      assert.ok(fallbacks.length >= 1, "the lazy fallback zone was read");
    }
    if (name === "barcodes") {
      for (const s of run.sheets) for (const z of Object.values(s.js.zones)) if (z.value) assert.equal(z.engine, "builtin");
    }
    if (name === "zones") {
      for (const s of run.sheets) {
        for (const zone of ["sheet_id", "qr"]) {
          assert.equal(s.js.zones[zone].value, s.py.zones[zone].value, `${s.fileId} ${zone}`);
          if (!s.js.zones[zone].needs_review) assert.equal(s.js.zones[zone].value, s.truth[zone]);
        }
        assert.equal(s.js.status, s.py.status, s.fileId);
      }
    }
  });
}

test("parity: repository samples (CropPage, CropOnMarkers, custom labels)", { skip: skip || !process.env.OMR_PARITY_SAMPLES }, async () => {
  const index = JSON.parse(fs.readFileSync(path.join(fixtures, "index.json"), "utf8"));
  for (const name of index.samples || []) {
    const st = H.compare(await H.runScenario(path.join(fixtures, name)));
    report.push(H.summary(name, st));
    assert.ok(st.valueAgreement >= 0.995, `${name}: value agreement ${st.valueAgreement}`);
  }
});

// ---------------------------------------------------------------- building blocks

function units() {
  const file = fixtures && path.join(fixtures, "units.json");
  return file && fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : null;
}
const bytes = (b64) => new Uint8Array(Buffer.from(b64, "base64"));

test("colour dropout matches src/color.py byte for byte", { skip }, (t) => {
  const u = units();
  if (!u) return t.skip("no units.json (regenerate fixtures)");
  const d = u.dropout;
  const px = { width: d.width, height: d.height, data: bytes(d.rgb), channels: 3 };
  for (const c of d.cases) {
    const got = H.OMR._internals.applyDropout(px, c.spec).data;
    const want = bytes(c.gray);
    let diff = 0;
    for (let i = 0; i < want.length; i++) if (got[i] !== want[i]) diff++;
    assert.equal(diff, 0, `${JSON.stringify(c.spec)}: ${diff} pixels differ`);
  }
  // RGBA input (what a canvas gives) reads the same
  const rgba = new Uint8Array(d.width * d.height * 4);
  for (let i = 0; i < d.width * d.height; i++) {
    rgba.set(px.data.subarray(i * 3, i * 3 + 3), i * 4);
    rgba[i * 4 + 3] = 255;
  }
  const spec = d.cases[d.cases.length - 1].spec;
  assert.deepEqual(H.OMR._internals.applyDropout({ width: d.width, height: d.height, data: rgba, channels: 4 }, spec).data, bytes(d.cases[d.cases.length - 1].gray));
});

test("thick ellipse outlines match cv2.ellipse(thickness=2)", { skip }, (t) => {
  const u = units();
  if (!u) return t.skip("no units.json (regenerate fixtures)");
  for (const e of u.ellipses) {
    const mask = new Uint8Array(e.w * e.h);
    H.OMR._internals.ellipseOutline(mask, e.w, e.h, e.cx, e.cy, e.ax, e.ay);
    const want = bytes(e.mask);
    let diff = 0;
    for (let i = 0; i < want.length; i++) if (!mask[i] !== !want[i]) diff++;
    assert.equal(diff, 0, `${JSON.stringify([e.w, e.h, e.cx, e.cy, e.ax, e.ay])}: ${diff} pixels differ`);
  }
});

test("built-in linear decoder matches src/readers/linear.py", { skip }, (t) => {
  const u = units();
  if (!u) return t.skip("no units.json (regenerate fixtures)");
  let decoded = 0;
  for (const c of u.linear) {
    const gray = H.OMR._internals.makeImage(c.width, c.height, bytes(c.gray));
    const got = H.OMR._internals.decodeLinear(gray, c.formats, {});
    assert.deepEqual(got, c.python, `${c.format} ${JSON.stringify(c.formats)}`);
    if (got) decoded++;
  }
  assert.ok(decoded >= u.linear.length / 2, `only ${decoded}/${u.linear.length} decoded`);
});

test.after(() => {
  if (report.length) console.log("\n" + report.join("\n"));
});
