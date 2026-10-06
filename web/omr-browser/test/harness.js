"use strict";
// Shared helpers for the parity tests: fixture generation, PGM decoding and
// comparison of browser-engine results with ground truth and the Python engine.
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { execFileSync } = require("node:child_process");
const OMR = require("../omr.js");

const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");

// Binary PGM (gray) or PPM (RGB -> {width, height, data, channels: 3})
function readPgm(file) {
  const buf = fs.readFileSync(file);
  let i = 0;
  const fields = [];
  while (fields.length < 4) {
    while (/\s/.test(String.fromCharCode(buf[i]))) i++;
    if (buf[i] === 0x23) {
      while (buf[i] !== 0x0a) i++;
      continue;
    }
    const start = i;
    while (!/\s/.test(String.fromCharCode(buf[i]))) i++;
    fields.push(buf.toString("latin1", start, i));
  }
  i++;
  if (fields[0] !== "P5" && fields[0] !== "P6") throw new Error(`${file}: not a binary PGM/PPM`);
  const width = Number(fields[1]);
  const height = Number(fields[2]);
  const channels = fields[0] === "P6" ? 3 : 1;
  const data = new Uint8Array(buf.subarray(i, i + width * height * channels));
  return channels === 3 ? { width, height, data, channels } : { width, height, data };
}

function sheetImage(dir, fileId) {
  const ppm = path.join(dir, `${fileId}.ppm`);
  return readPgm(fs.existsSync(ppm) ? ppm : path.join(dir, `${fileId}.pgm`));
}

function findPython() {
  for (const candidate of [process.env.PYTHON, "python3", "python"].filter(Boolean)) {
    try {
      execFileSync(candidate, ["-c", "import cv2, numpy"], { stdio: "ignore", cwd: REPO_ROOT });
      return candidate;
    } catch (_) {
      /* try next */
    }
  }
  return null;
}

// Fixtures come from OMR_FIXTURES_DIR (pre-generated) or are rendered now into a temp dir
function ensureFixtures(n = 6) {
  const existing = process.env.OMR_FIXTURES_DIR;
  if (existing && fs.existsSync(path.join(existing, "index.json"))) return existing;
  const python = findPython();
  if (!python) return null;
  const out = existing || fs.mkdtempSync(path.join(os.tmpdir(), "omr_browser_fixtures_"));
  execFileSync(python, [path.join(__dirname, "make_fixtures.py"), "--out", out, "--n", String(n)], {
    cwd: REPO_ROOT,
    stdio: ["ignore", "ignore", "inherit"],
    env: { ...process.env, PYTHONPATH: REPO_ROOT },
  });
  return out;
}

function fieldLabels(templateJson) {
  const parsed = OMR._internals.parseTemplate(templateJson);
  const labels = [];
  parsed.fieldBlocks.forEach((b) => b.labels.forEach((l) => labels.push(l)));
  return labels;
}

async function runScenario(dir, options = {}) {
  const templateJson = JSON.parse(fs.readFileSync(path.join(dir, "template.json"), "utf8"));
  const expected = JSON.parse(fs.readFileSync(path.join(dir, "expected.json"), "utf8"));
  const configPath = path.join(dir, "config.json");
  const assetsPath = path.join(dir, "assets.json");
  const loadOptions = { ...options };
  if (fs.existsSync(configPath)) loadOptions.config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  if (fs.existsSync(assetsPath)) {
    const assets = JSON.parse(fs.readFileSync(assetsPath, "utf8"));
    loadOptions.assets = {};
    for (const [name, file] of Object.entries(assets)) loadOptions.assets[name] = readPgm(path.join(dir, file));
  }
  const engine = await OMR.loadTemplate(templateJson, loadOptions);
  const labels = fieldLabels(templateJson);
  const sheets = [];
  for (const fileId of Object.keys(expected)) {
    const image = sheetImage(dir, fileId);
    const t0 = process.hrtime.bigint();
    const js = await engine.scan(image, { fileId });
    const ms = Number(process.hrtime.bigint() - t0) / 1e6;
    sheets.push({ fileId, js, py: expected[fileId].python, truth: expected[fileId].truth, ms });
  }
  return { dir, labels, templateJson, sheets };
}

// JSON with object keys sorted
function canonical(value) {
  return JSON.stringify(value, (key, v) =>
    v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, v[k]])) : v
  );
}

function compare(run) {
  const stats = {
    sheets: run.sheets.length,
    jsErrors: 0,
    pyErrors: 0,
    bothRead: 0,
    fields: 0,
    valueAgree: 0,
    reviewAgree: 0,
    statusAgree: 0,
    unflaggedJs: 0,
    unflaggedJsWrong: 0,
    unflaggedPy: 0,
    unflaggedPyWrong: 0,
    jsCorrect: 0,
    pyCorrect: 0,
    msTotal: 0,
    msMax: 0,
    disagreements: [],
    truthErrors: [],
    zones: 0,
    zoneAgree: 0,
    zoneDisagreements: [],
    rulesAgree: 0,
    rulesDisagreements: [],
    reviewListAgree: 0,
    thresholdsAgree: 0,
    reviewListDisagreements: [],
    sheetItems: 0,
  };
  for (const s of run.sheets) {
    stats.msTotal += s.ms;
    stats.msMax = Math.max(stats.msMax, s.ms);
    if (s.js.status === "error") stats.jsErrors++;
    if (s.py.status === "error") stats.pyErrors++;
    if (s.js.status === s.py.status) stats.statusAgree++;
    if (s.js.status === "error" || s.py.status === "error") continue;
    stats.bothRead++;
    // Zones: value, flags, review and the engine that decoded
    for (const name of Object.keys(s.py.zones)) {
      const j = s.js.zones[name] || {};
      const p = s.py.zones[name];
      stats.zones++;
      const same = j.value === p.value && j.needs_review === p.needs_review && j.engine === p.engine && JSON.stringify(j.flags) === JSON.stringify(p.flags);
      if (same) stats.zoneAgree++;
      else stats.zoneDisagreements.push({ file: s.fileId, name, js: [j.value, j.engine, j.flags], py: [p.value, p.engine, p.flags] });
    }
    // Rules: checks, validation and the output row must match exactly
    // (key order is not compared: Python builds responses from a set)
    const rulesJs = canonical([s.js.checks, s.js.validation, s.js.responses]);
    const rulesPy = canonical([s.py.checks, s.py.validation, s.py.responses]);
    if (rulesJs === rulesPy) stats.rulesAgree++;
    else stats.rulesDisagreements.push({ file: s.fileId, js: rulesJs, py: rulesPy });
    if (canonical(s.js.thresholds) === canonical(s.py.thresholds)) stats.thresholdsAgree++;
    // The whole review list (field, zone, rule and sheet items) in order
    if (canonical(s.js.review) === canonical(s.py.review)) stats.reviewListAgree++;
    else stats.reviewListDisagreements.push({ file: s.fileId, js: canonical(s.js.review), py: canonical(s.py.review) });
    stats.sheetItems += s.py.review.filter((item) => item.kind === "sheet").length;
    for (const label of run.labels) {
      const j = s.js.fields[label];
      const p = s.py.fields[label];
      const truth = s.truth ? s.truth[label] : undefined;
      stats.fields++;
      if (j.value === p.value) stats.valueAgree++;
      else stats.disagreements.push({ file: s.fileId, label, js: j.value, py: p.value, truth, jsFlags: j.flags, pyFlags: p.flags });
      if (j.needs_review === p.needs_review) stats.reviewAgree++;
      if (!s.truth) continue;
      if (j.value === truth) stats.jsCorrect++;
      if (p.value === truth) stats.pyCorrect++;
      if (!j.needs_review) {
        stats.unflaggedJs++;
        if (j.value !== truth) {
          stats.unflaggedJsWrong++;
          stats.truthErrors.push({ file: s.fileId, label, js: j.value, truth, flags: j.flags });
        }
      }
      if (!p.needs_review) {
        stats.unflaggedPy++;
        if (p.value !== truth) stats.unflaggedPyWrong++;
      }
    }
  }
  stats.valueAgreement = stats.fields ? stats.valueAgree / stats.fields : 1;
  stats.reviewAgreement = stats.fields ? stats.reviewAgree / stats.fields : 1;
  stats.msMean = stats.sheets ? stats.msTotal / stats.sheets : 0;
  stats.zoneAgreement = stats.zones ? stats.zoneAgree / stats.zones : 1;
  return stats;
}

function summary(name, st) {
  return (
    `${name}: sheets=${st.sheets} jsErr=${st.jsErrors} pyErr=${st.pyErrors} fields=${st.fields} ` +
    `value-agree=${(100 * st.valueAgreement).toFixed(2)}% review-agree=${(100 * st.reviewAgreement).toFixed(2)}% ` +
    `status-agree=${st.statusAgree}/${st.sheets} unflagged-wrong js=${st.unflaggedJsWrong}/${st.unflaggedJs} py=${st.unflaggedPyWrong}/${st.unflaggedPy} ` +
    `acc js=${st.jsCorrect} py=${st.pyCorrect} ` +
    (st.zones ? `zones=${st.zoneAgree}/${st.zones} ` : "") +
    `rules=${st.rulesAgree}/${st.bothRead} review-lists=${st.reviewListAgree}/${st.bothRead} ` +
    `ms/sheet mean=${st.msMean.toFixed(1)} max=${st.msMax.toFixed(1)}`
  );
}

module.exports = { readPgm, sheetImage, canonical, ensureFixtures, runScenario, compare, summary, REPO_ROOT, OMR };
