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
  assert.deepEqual(Object.keys(r), ["file_id", "status", "responses", "fields", "zones", "review", "score", "error", "thresholds", "timings_ms"]);
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
