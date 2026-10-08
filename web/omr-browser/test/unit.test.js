"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const OMR = require("../omr.js");
const I = OMR._internals;

test("field strings expand like src/utils/parsing.py", () => {
  const t = I.parseTemplate({
    pageDimensions: [300, 300],
    bubbleDimensions: [10, 10],
    fieldBlocks: { A: { fieldType: "QTYPE_MCQ4", origin: [10, 10], bubblesGap: 20, labelsGap: 20, fieldLabels: ["q1..3"] } },
    customLabels: { all: ["q1", "q2"] },
  });
  assert.deepEqual(t.fieldBlocks[0].labels, ["q1", "q2", "q3"]);
  assert.deepEqual(t.fieldBlocks[0].fields[1].bubbles.map((b) => [b.x, b.y, b.value]), [[10, 30, "A"], [30, 30, "B"], [50, 30, "C"], [70, 30, "D"]]);
  assert.deepEqual(t.nonCustomLabels, ["q3"]);
  assert.deepEqual(t.outputColumns, ["all", "q3"]);
  assert.throws(() => I.parseTemplate({ pageDimensions: [50, 50], bubbleDimensions: [10, 10], fieldBlocks: { A: { fieldType: "QTYPE_MCQ4", origin: [10, 10], bubblesGap: 20, labelsGap: 20, fieldLabels: ["q1"] } } }), /Overflowing/);
});

test("homography: exact 4-point, least squares and RANSAC with outliers", () => {
  const H = [1.1, 0.05, 20, -0.03, 0.95, 40, 2e-5, -1e-5, 1];
  const src = [];
  for (let y = 0; y < 5; y++) for (let x = 0; x < 6; x++) src.push([x * 100, y * 120]);
  const project = (p) => {
    const w = H[6] * p[0] + H[7] * p[1] + 1;
    return [(H[0] * p[0] + H[1] * p[1] + H[2]) / w, (H[3] * p[0] + H[4] * p[1] + H[5]) / w];
  };
  const dst = src.map(project);
  const idx = [0, 5, 29, 24];
  const four = I.getPerspectiveTransform(idx.map((i) => src[i]), idx.map((i) => dst[i]));
  four.forEach((v, i) => assert.ok(Math.abs(v - H[i]) < 1e-6 * (1 + Math.abs(H[i]))));
  assert.ok(four);
  const ls = I.findHomographyLS(src, dst);
  ls.forEach((v, i) => assert.ok(Math.abs(v - H[i]) < 1e-6 * (1 + Math.abs(H[i]))));
  const noisy = dst.map((p, i) => (i % 7 === 3 ? [p[0] + 80, p[1] - 50] : p));
  const r = I.findHomographyRansac(src, noisy, 3, 1);
  assert.equal(r.inliers.length, src.filter((_, i) => i % 7 !== 3).length);
  r.H.forEach((v, i) => assert.ok(Math.abs(v - H[i]) < 1e-5 * (1 + Math.abs(H[i]))));
});

test("thresholds follow core.py", () => {
  const tp = OMR.CONFIG_DEFAULTS.threshold_params;
  // clear jump between marked (~40) and empty (~210) bubbles
  assert.equal(I.getLocalThreshold([210, 205, 40, 212], 150, false, tp).threshold, 125);
  // no jump, no outliers -> global threshold
  const flat = I.getLocalThreshold([210, 205, 208, 212], 150, true, tp);
  assert.equal(flat.threshold, 150);
  assert.equal(flat.lowConfidence, false);
  // no jump but outliers -> flagged
  assert.equal(I.getLocalThreshold([210, 195, 208, 212], 150, false, tp).lowConfidence, true);
});

test("rank filter matches brute force", () => {
  const img = I.makeImage(23, 17);
  for (let i = 0; i < img.data.length; i++) img.data[i] = (i * 2654435761) >>> 24;
  const out = I.rankFilter(img, 2, 3, 1, 4, false).data;
  for (let y = 0; y < 17; y++)
    for (let x = 0; x < 23; x++) {
      let v = 255;
      for (let yy = Math.max(0, y - 1); yy <= Math.min(16, y + 4); yy++) for (let xx = Math.max(0, x - 2); xx <= Math.min(22, x + 3); xx++) v = Math.min(v, img.data[yy * 23 + xx]);
      assert.equal(out[y * 23 + x], v);
    }
});

test("unsupported preprocessors are rejected with a clear error", async () => {
  await assert.rejects(
    OMR.loadTemplate({ pageDimensions: [100, 100], bubbleDimensions: [10, 10], fieldBlocks: {}, preProcessors: [{ name: "FeatureBasedAlignment", options: { reference: "x.jpg" } }] }),
    /not supported in the browser/
  );
});

test("a blank image fails registration with the Python error message", async () => {
  const engine = await OMR.loadTemplate({
    pageDimensions: [400, 400], bubbleDimensions: [20, 20],
    preProcessors: [{ name: "TimingMarkAlignment", options: { markDimensions: [20, 10], tracks: { left: { marks: [[20, 50], [20, 100], [20, 150], [20, 200], [20, 250]] }, top: { marks: [[100, 20], [150, 20], [200, 20], [250, 20]] } } } }],
    fieldBlocks: { A: { fieldType: "QTYPE_MCQ4", origin: [100, 100], bubblesGap: 30, labelsGap: 30, fieldLabels: ["q1"] } },
  });
  const r = await engine.scan({ width: 400, height: 400, data: new Uint8Array(400 * 400).fill(255) });
  assert.equal(r.status, "error");
  assert.match(r.error, /registration failed/);
  assert.deepEqual(Object.keys(r), ["file_id", "status", "responses", "fields", "zones", "review", "score", "error", "thresholds", "timings_ms", "checks", "validation"]);
});

test("OCR/ICR zones without a reader are flagged engine_unavailable; a hook is used when registered", async () => {
  const tpl = {
    pageDimensions: [300, 300], bubbleDimensions: [20, 20],
    fieldBlocks: { A: { fieldType: "QTYPE_MCQ4", origin: [20, 200], bubblesGap: 30, labelsGap: 30, fieldLabels: ["q1"] } },
    zones: { name: { type: "ocr", origin: [10, 10], dimensions: [100, 40], options: { pattern: "[A-Z]+" } }, cand: { type: "icr", origin: [10, 60], dimensions: [100, 40] } },
  };
  const img = { width: 300, height: 300, data: new Uint8Array(300 * 300).fill(230) };
  let engine = await OMR.loadTemplate(tpl);
  let r = await engine.scan(img);
  assert.deepEqual(r.zones.name.flags, ["engine_unavailable"]);
  assert.deepEqual(r.zones.cand.flags, ["engine_unavailable", "no_icr_model"]);
  assert.equal(r.status, "needs_review");
  engine = (await OMR.loadTemplate(tpl)).setZoneReader("ocr", async (crop) => ({ value: crop.width > 0 ? "ABC" : "", confidence: 0.9 }));
  r = await engine.scan(img);
  assert.equal(r.zones.name.value, "ABC");
  assert.deepEqual(r.zones.name.flags, []);
  assert.equal(r.responses.name, "ABC");
});

const SMALL = {
  pageDimensions: [200, 120], bubbleDimensions: [16, 16],
  fieldBlocks: { A: { fieldType: "QTYPE_MCQ4", origin: [20, 40], bubblesGap: 30, labelsGap: 30, fieldLabels: ["q1", "q2"] } },
};

test("colour dropout API: needsColor, setColorDropout, templateOverrides and per-zone settings", async () => {
  const grey = await OMR.loadTemplate(SMALL);
  assert.equal(grey.needsColor, false);
  grey.setColorDropout({ mode: "red", strength: 0.5 });
  assert.equal(grey.needsColor, true);
  grey.setColorDropout(null);
  assert.equal(grey.needsColor, false);
  const red = await OMR.loadTemplate(Object.assign({ colorDropout: "red" }, SMALL));
  assert.equal(red.needsColor, true);
  const overridden = await OMR.loadTemplate(Object.assign({ colorDropout: "red" }, SMALL), { templateOverrides: { colorDropout: null } });
  assert.equal(overridden.needsColor, false);
  const zoneOnly = await OMR.loadTemplate(
    Object.assign({ zones: { id: { type: "barcode", origin: [10, 5], dimensions: [150, 30], options: { formats: ["Code128"], colorDropout: "max" } } } }, SMALL)
  );
  assert.equal(zoneOnly.needsColor, true);
  assert.throws(() => I.normalizeDropout({ mode: "purple" }), /Unknown colorDropout mode/);

  // Pink print is dropped by the red channel
  const w = 200, h = 120, rgb = new Uint8Array(w * h * 3).fill(255);
  for (let y = 40; y < 56; y++) for (let x = 20; x < 140; x++) rgb.set([0xe8, 0x61, 0x8c], (y * w + x) * 3);
  const out = I.applyDropout({ width: w, height: h, data: rgb, channels: 3 }, "red");
  assert.equal(out.data[45 * w + 30], 0xe8);
  const r = await red.scan({ width: w, height: h, data: rgb, channels: 3 }, { fileId: "pink" });
  assert.ok("dropout" in r.timings_ms);
});

test("checkTemplate: built-in decoder formats need no zxing-wasm; QR codes do", () => {
  if (OMR.barcodesAvailable()) return;
  const zones = {
    a: { type: "barcode", origin: [0, 0], dimensions: [100, 40], options: { formats: ["Code128"] } },
    b: { type: "qrcode", origin: [0, 50], dimensions: [40, 40] },
  };
  assert.deepEqual(OMR.checkTemplate(Object.assign({ zones }, SMALL)).issues, ["Zone 'b' needs OMR.enableBarcodes()"]);
});

test("rules: Python's errors for bad specs, and results on a blank sheet", async () => {
  const withRules = (extra) => OMR.loadTemplate(Object.assign({}, SMALL, extra));
  await assert.rejects(withRules({ checks: [{ name: "c", sources: ["q1", "nope"] }] }), /check 'c': unknown source 'nope' \(not a field, custom label, zone or check\)/);
  await assert.rejects(withRules({ validate: { q1: { onFail: "explode" } } }), /onFail must be one of/);
  const engine = await withRules({
    validate: { q1: { required: true, onFail: "both" }, q2: { pattern: "[A-B]", onFail: "flag" } },
    checks: [{ name: "pair", sources: ["q1", "q2"] }],
  });
  assert.deepEqual(engine.template.outputColumns, ["pair", "q1", "q2"]); // natural sort, as Python
  const r = await engine.scan({ width: 200, height: 120, data: new Uint8Array(200 * 120).fill(255) });
  assert.deepEqual(r.validation.q1, { ok: false, kind: "field", value: "", reasons: ["empty"], action: "both" });
  assert.equal(r.checks.pair.flags[0], "all_sources_missing");
  assert.equal(r.status, "needs_review");
  assert.ok(r.review.some((item) => item.name === "q1" && item.flags.includes("validation_failed")));
});

test("fixed threshold mode, and Python repr() in validation reasons", async () => {
  const engine = await OMR.loadTemplate(Object.assign({ validate: { q1: { pattern: "it's\\d" } } }, SMALL), {
    config: { threshold_params: { mode: "fixed", fixed_threshold: 140 } },
  });
  const img = new Uint8Array(200 * 120).fill(255);
  for (let y = 42; y < 54; y++) for (let x = 22; x < 34; x++) img[y * 200 + x] = 20; // q1 = A
  const r = await engine.scan({ width: 200, height: 120, data: img });
  assert.equal(r.thresholds.mode, "fixed");
  assert.equal(r.responses.q1, "A");
  assert.deepEqual(r.validation.q1.reasons, ["does not match \"it's\\\\d\""]);
});

test("group placeholders (groupOptions) follow src/utils/parsing.py", async () => {
  const { joinGroup, columnState } = OMR._internals;
  const col = (value, flags, review, reviewed) => ({ value, flags: flags || (value ? [] : ["empty"]), needs_review: !!review, reviewed: !!reviewed });
  const fields = { r1: col("0"), r2: col(""), r3: col("12", ["multi_marked"], true), r4: col("4", ["low_confidence"], true), r5: col("5"), r6: col("6") };
  const omr = {};
  Object.keys(fields).forEach((k) => (omr[k] = fields[k].value));
  const cols = ["r1", "r2", "r3", "r4", "r5", "r6"];
  const defaults = { empty: " ", multi: "*", issue: "-" };
  assert.deepEqual(joinGroup(cols, omr, defaults, fields, ""), ["0 *-56", ["ok", "empty", "multi", "issue", "ok", "ok"]]);
  assert.equal(joinGroup(cols, omr, { empty: null, multi: "*", issue: "-" }, fields, "")[0], "0*-56");
  assert.equal(joinGroup(cols, omr, null, fields, "")[0], "012456");
  assert.equal(columnState("", col("", ["empty", "possible_missed_mark"], true)), "issue");
  assert.equal(columnState("12", col("12", ["multi_marked"], false, true)), "ok");

  // A blank sheet: empty columns keep their place; old templates keep the plain join
  const blank = { width: 200, height: 120, data: new Uint8Array(200 * 120).fill(255) };
  const plain = await OMR.loadTemplate(Object.assign({ customLabels: { both: ["q1", "q2"] } }, SMALL));
  assert.equal((await plain.scan(blank)).responses.both, "");
  const grouped = await OMR.loadTemplate(Object.assign({ customLabels: { both: ["q1", "q2"] }, groupOptions: { both: { empty: "_" } } }, SMALL));
  const r = await grouped.scan(blank);
  assert.equal(r.responses.both, "__");
  assert.deepEqual(r.groups.both.columns.map((c) => c.state), ["empty", "empty"]);
  assert.equal(r.groups.both.flagged, false);
});
