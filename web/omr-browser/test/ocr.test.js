"use strict";
// Optional OCR readers (omr-ocr.js): direction, fallback, CTC decode, image zones
const test = require("node:test");
const assert = require("node:assert/strict");
const OMR = require("../omr.js");
const OCR = require("../omr-ocr.js");

const img = (w, h) => ({ width: w, height: h, data: Uint8Array.from({ length: w * h }, (_, i) => i) });

test("rotateGray matches cv2.rotate", () => {
  const a = img(3, 2); // [[0,1,2],[3,4,5]]
  assert.deepEqual([...OCR.rotateGray(a, "rot90cw").data], [3, 0, 4, 1, 5, 2]);
  assert.deepEqual([...OCR.rotateGray(a, "rot90ccw").data], [2, 5, 1, 4, 0, 3]);
  assert.deepEqual([...OCR.rotateGray(a, "rot180").data], [5, 4, 3, 2, 1, 0]);
  assert.equal(OCR.rotateGray(a, "rot90cw").width, 2);
  assert.equal(OCR.rotateGray(a, "horizontal"), a);
});

test("auto direction keeps the best valid read", async () => {
  const upright = OCR.rotateGray(img(4, 2), "rot90cw");
  const reader = OCR.withDirection((crop) => ({ value: crop.width === 2 && crop.data[0] === upright.data[0] ? "1234" : "", confidence: 0.9 }));
  const zone = { options: { direction: "auto", pattern: "[0-9]{4}" } };
  const out = await reader(img(4, 2), zone);
  assert.equal(out.value, "1234");
  assert.equal(out.details.direction, "rot90cw");
  assert.equal(out.details.reads.length, 4);
});

test("fallback on invalid read, disagreement flagged", async () => {
  const zone = { options: { pattern: "[0-9]{3}" } };
  const r = OCR.withFallback(() => ({ value: "12a", confidence: 0.9, engine: "t" }), () => ({ value: "123", confidence: 0.8, engine: "p" }));
  const out = await r(img(2, 2), zone);
  assert.equal(out.value, "123");
  assert.deepEqual(out.flags, ["engine_disagree"]);
  const good = OCR.withFallback(() => ({ value: "123", confidence: 0.9 }), () => assert.fail("not called"));
  assert.equal((await good(img(2, 2), zone)).value, "123");
});

test("CTC decode like paddle_ocr.ctc_decode", () => {
  const chars = ["<blank>", "0", "1", "2", " "];
  const seq = [1, 1, 0, 2, 2, 0, 2, 4, 3];
  const probs = new Float32Array(seq.length * 5).fill(0.01);
  seq.forEach((c, t) => (probs[t * 5 + c] = 0.9));
  assert.equal(OCR.ctcDecode(probs, seq.length, 5, chars).text, "011 2");
  assert.equal(OCR.recTensor(img(20, 10)).dims.join(), "1,3,48,96");
});

test("image zones are read without review", async () => {
  const page = OMR._internals.makeImage(100, 100, new Uint8Array(10000).fill(200));
  const zone = { name: "photo", type: "image", origin: [10, 10], dimensions: [30, 20], options: {}, emptyValue: "" };
  const r = await OMR._internals.readZone(zone, page, {}, {});
  assert.equal(r.needs_review, false);
  assert.deepEqual(r.details, { width: 30, height: 20 });
  assert.equal(r.crop.width, 30);
  assert.ok(!("crop" in JSON.parse(JSON.stringify(r))));
});
