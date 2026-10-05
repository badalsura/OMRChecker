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

for (const name of ["clean", "scan", "phone", "croppage", "zones"]) {
  test(`parity: ${name}`, { skip }, async (t) => {
    const dir = path.join(fixtures, name);
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

test.after(() => {
  if (report.length) console.log("\n" + report.join("\n"));
});
