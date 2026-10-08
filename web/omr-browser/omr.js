/*!
 * omr.js - in-browser OMR engine (port of the OMRChecker Python engine)
 *
 * Dependency-free plain JavaScript (ES2017). Works as:
 *   - a classic <script src="omr.js"> (defines window.OMR)
 *   - an ES module side-effect import (see omr.mjs for named exports)
 *   - a CommonJS module in Node (require('./omr.js'))
 *   - inside a Web Worker via importScripts (see omr.worker.js)
 *
 * Optional, lazy-loaded engines (never bundled):
 *   - zxing-wasm (ZXing-C++ WebAssembly) for barcode / QR zones
 *   - onnxruntime-web for the learned bubble classifier
 *   - any OCR/ICR function you register (e.g. tesseract.js)
 */
(function (root, factory) {
  var OMR = factory(root);
  if (typeof module === "object" && module && module.exports) module.exports = OMR;
  if (root) root.OMR = OMR;
})(
  typeof globalThis !== "undefined" ? globalThis : typeof self !== "undefined" ? self : typeof window !== "undefined" ? window : undefined,
  function (root) {
    "use strict";

    var VERSION = "1.0.0";

    // ------------------------------------------------------------------------
    // Defaults (mirror src/defaults/config.py and src/constants)
    // ------------------------------------------------------------------------
    var CONFIG_DEFAULTS = {
      dimensions: { display_height: 2480, display_width: 1640, processing_height: 820, processing_width: 666 },
      threshold_params: { GAMMA_LOW: 0.7, MIN_GAP: 30, MIN_JUMP: 25, CONFIDENT_SURPLUS: 5, JUMP_DELTA: 30, PAGE_TYPE_FOR_THRESHOLD: "white", mode: "adaptive", fixed_threshold: 120, fixed_min_fill_ratio: 0.12, flatten_background: true },
      alignment_params: { auto_align: false, match_col: 5, max_steps: 20, stride: 1, thickness: 3, block_snap_radius: 0, rectify_on_border: false, rectify_search_px: 20 },
      review_params: {
        confidence_margin: 20,
        min_confidence: 0.35,
        min_marked_fill_ratio: 0.25,
        max_unmarked_fill_ratio: 0.6,
        min_marked_bubbles: 0,
        review_flags: ["multi_marked", "ambiguous_threshold", "low_confidence", "weak_mark", "possible_missed_mark", "model_disagrees"],
      },
      ml_params: { bubble_model_path: null, icr_model_path: null },
      // "opencv" and "pyzbar" are Python-only engines and are skipped here
      barcode_params: { engines: ["zxing", "builtin", "opencv", "pyzbar"], pyzbar: false, review_fallback_decodes: false },
      outputs: { show_image_level: 0, save_image_level: 0, save_detections: true, filter_out_multimarked_files: false },
    };
    var GLOBAL_PAGE_THRESHOLD_WHITE = 200;
    var GLOBAL_PAGE_THRESHOLD_BLACK = 100;
    var FIELD_TYPES = {
      QTYPE_INT: { bubbleValues: ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"], direction: "vertical" },
      QTYPE_INT_FROM_1: { bubbleValues: ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"], direction: "vertical" },
      QTYPE_MCQ4: { bubbleValues: ["A", "B", "C", "D"], direction: "horizontal" },
      QTYPE_MCQ5: { bubbleValues: ["A", "B", "C", "D", "E"], direction: "horizontal" },
      QTYPE_MCQ4_RTL: { bubbleValues: ["D", "C", "B", "A"], direction: "horizontal" },
      QTYPE_MCQ5_RTL: { bubbleValues: ["E", "D", "C", "B", "A"], direction: "horizontal" },
    };
    var ZONE_REVIEW_FLAGS = ["not_found", "multiple_symbols", "low_confidence", "pattern_mismatch", "engine_unavailable", "no_icr_model", "read_error", "engine_disagree", "low_char_confidence"];
    var STATUS_OK = "ok", STATUS_NEEDS_REVIEW = "needs_review", STATUS_ERROR = "error";
    var REGISTRATION_ERROR = "Sheet registration failed (page, markers or timing marks not found)";
    // TimingMarkAlignment
    var TM_DEFAULT_SIZE_TOLERANCE = 0.5, TM_DEFAULT_MIN_MATCHED = 8, TM_DEFAULT_MAX_RESIDUAL = 3.0, TM_MIN_PAGE_AREA_FRACTION = 0.3, TPS_GRID_STEP = 16;
    // A fit matching this share of marks with none past the track ends is final
    var TM_GOOD_FIT_FRACTION = 0.95, TM_MIN_TILT_DEGREES = 0.3;
    // CropPage
    var MIN_PAGE_AREA_THRESHOLD = 80000, MAX_COSINE_THRESHOLD = 0.35, APPROX_POLY_EPSILON_FACTOR = 0.025;

    var DEFAULT_LIBS = {
      zxingScriptUrl: "https://cdn.jsdelivr.net/npm/zxing-wasm@3.1.4/dist/iife/reader/index.js",
      zxingWasmUrl: "https://cdn.jsdelivr.net/npm/zxing-wasm@3.1.4/dist/reader/zxing_reader.wasm",
      ortScriptUrl: "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/ort.min.js",
      ortWasmPaths: "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/",
    };

    // ------------------------------------------------------------------------
    // Small helpers
    // ------------------------------------------------------------------------
    function now() {
      return typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
    }
    function elapsed(start) {
      return Math.round((now() - start) * 10) / 10;
    }
    function isObject(v) {
      return v !== null && typeof v === "object" && !Array.isArray(v);
    }
    function deepMerge(base, over) {
      var out = {};
      var k;
      for (k in base) out[k] = isObject(base[k]) ? deepMerge(base[k], {}) : Array.isArray(base[k]) ? base[k].slice() : base[k];
      if (over) for (k in over) out[k] = isObject(over[k]) && isObject(out[k]) ? deepMerge(out[k], over[k]) : over[k];
      return out;
    }
    // Python's round(): half to even
    function roundHalfEven(x) {
      var r = Math.round(x);
      if (Math.abs(x % 1) === 0.5 && r % 2 !== 0) r -= 1;
      return r;
    }
    function roundTo(x, digits) {
      var f = Math.pow(10, digits);
      var y = x * f;
      var r = Math.round(y);
      if (Math.abs(y - Math.trunc(y)) === 0.5 && r % 2 !== 0) r -= 1;
      return r / f;
    }
    // OpenCV cvRound: round half to even
    var cvRound = roundHalfEven;
    function clamp(v, lo, hi) {
      return v < lo ? lo : v > hi ? hi : v;
    }
    function floorDiv(v, s) {
      // arithmetic right shift by s bits for (possibly > 32-bit) integers
      return Math.floor(v / Math.pow(2, s));
    }
    function mulberry32(seed) {
      var a = seed >>> 0;
      return function () {
        a = (a + 0x6d2b79f5) >>> 0;
        var t = a;
        t = Math.imul(t ^ (t >>> 15), t | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
      };
    }

    // ------------------------------------------------------------------------
    // Gray images: {width, height, data: Uint8Array|Uint8ClampedArray}
    // ------------------------------------------------------------------------
    function makeImage(width, height, data) {
      return { width: width, height: height, data: data || new Uint8Array(width * height) };
    }
    function cloneImage(img) {
      return makeImage(img.width, img.height, new Uint8Array(img.data));
    }
    // RGBA -> gray with OpenCV's fixed-point BT.601 weights
    function rgbaToGray(rgba, width, height) {
      var n = width * height;
      var out = new Uint8Array(n);
      for (var i = 0, j = 0; i < n; i++, j += 4) {
        out[i] = (rgba[j] * 9798 + rgba[j + 1] * 19235 + rgba[j + 2] * 3735 + 16384) >> 15;
      }
      return makeImage(width, height, out);
    }

    // ------------------------------------------------------------------------
    // Colour dropout (port of src/color.py). Pixel buffers are
    // {width, height, data, channels: 3 | 4} in RGB(A) order.
    // ------------------------------------------------------------------------
    var fr = Math.fround;
    var DROPOUT_MODES = ["grey", "red", "green", "blue", "max", "color"];
    var DROPOUT_TOLERANCE = 60.0, LIGHTNESS_WEIGHT = 0.5, DROPOUT_FALLOFF = 1.5;
    function dropoutSpec(mode, color, tolerance, strength) {
      return { mode: mode, color: color, tolerance: tolerance, strength: strength, key: JSON.stringify([mode, color, tolerance, strength]) };
    }
    var GREY_DROPOUT = dropoutSpec("grey", null, 0.0, 1.0);
    // '#E8618C' / 'E8618C' / '#e86' -> [b, g, r]
    function parseHex(value) {
      var text = String(value).trim().replace(/^#+/, "");
      if (text.length === 3)
        text = text
          .split("")
          .map(function (c) {
            return c + c;
          })
          .join("");
      if (text.length !== 6 || !/^[0-9a-fA-F]{6}$/.test(text)) throw new Error("Not a hex colour: '" + value + "'");
      return [parseInt(text.slice(4, 6), 16), parseInt(text.slice(2, 4), 16), parseInt(text.slice(0, 2), 16)];
    }
    // Canonical spec from a template value (object, mode string or null), like normalize_dropout
    function normalizeDropout(spec) {
      if (spec === null || spec === undefined) return GREY_DROPOUT;
      if (spec.key && spec.mode) return spec;
      if (typeof spec === "string") spec = { mode: spec };
      var mode = String(spec.mode !== undefined ? spec.mode : "grey").toLowerCase();
      if (mode === "gray") mode = "grey";
      if (DROPOUT_MODES.indexOf(mode) < 0) throw new Error("Unknown colorDropout mode '" + mode + "'");
      var strength = clamp(Number(spec.strength !== undefined ? spec.strength : 1.0), 0.0, 1.0);
      if (mode === "grey" || strength <= 0) return GREY_DROPOUT;
      var color = null, tolerance = 0.0;
      if (mode === "color") {
        if (!spec.color) throw new Error("colorDropout mode 'color' needs a 'color' (#RRGGBB)");
        color = parseHex(spec.color);
        tolerance = Number(spec.tolerance !== undefined ? spec.tolerance : DROPOUT_TOLERANCE);
        if (tolerance <= 0) return GREY_DROPOUT;
      }
      return dropoutSpec(mode, color, tolerance, strength);
    }
    function dropoutToJson(spec) {
      spec = normalizeDropout(spec);
      var out = { mode: spec.mode };
      if (spec.mode === "color") {
        out.color = "#" + [spec.color[2], spec.color[1], spec.color[0]].map(function (v) {
          return (v < 16 ? "0" : "") + v.toString(16).toUpperCase();
        }).join("");
        out.tolerance = spec.tolerance;
      }
      if (spec.mode !== "grey") out.strength = spec.strength;
      return out;
    }

    // cv2.cvtColor(float32 BGR / 255, COLOR_BGR2Lab) reproduced bit for bit: OpenCV
    // interpolates a 33^3 table (built with its softfloat maths) for sRGB input.
    var F32BUF = new Float32Array(1), U32BUF = new Uint32Array(F32BUF.buffer);
    function softCbrt(x) {
      // OpenCV softfloat cbrt (Turkowski): rational approximation, truncated mantissa
      F32BUF[0] = x;
      var bits = U32BUF[0], ex = ((bits >>> 23) & 255) - 127, shx = ex % 3;
      shx -= shx >= 0 ? 3 : 0;
      ex = (ex - shx) / 3 - 1;
      var v = (1 + (bits & 0x7fffff) / 8388608) * Math.pow(2, shx);
      v = ((((45.2548339756803022511987494 * v + 192.2798368355061050458134625) * v + 119.1654824285581628956914143) * v + 13.43250139086239872172837314) * v + 0.1636161226585754240958355063) /
        ((((14.80884093219134573786480845 * v + 151.9714051044435648658557668) * v + 168.5254414101568283957668343) * v + 33.9905941350215598754191872) * v + 1);
      return (Math.floor(v * 16777216) / 16777216) * Math.pow(2, ex + 1);
    }
    var labLut = null;
    function buildLabLut() {
      var D = 33, c = [0.412453, 0.35758, 0.180423, 0.212671, 0.71516, 0.072169, 0.019334, 0.119193, 0.950227];
      var wx = 1 / 0.950456, wz = 1 / 1.088754;
      // Table axes are (blue, green, red), as in OpenCV's initLUTforLABLUVs16
      var S = [fr(c[2] * wx), fr(c[1] * wx), fr(c[0] * wx), fr(c[5]), fr(c[4]), fr(c[3]), fr(c[8] * wz), fr(c[7] * wz), fr(c[6] * wz)];
      var lthresh = fr(216 / 24389), lscale = fr(841 / 108), lbias = fr(16 / 116), f9033 = fr(24389 / 27);
      var gamma = new Float32Array(D), n, p, q, r;
      for (n = 0; n < D; n++) {
        var x = fr(n / 32);
        gamma[n] = x <= 809 / 20000 ? x / (323 / 25) : Math.pow((x + 11 / 200) / (1 + 11 / 200), 12 / 5);
      }
      function f(t) {
        return t > lthresh ? softCbrt(t) : fr(t * lscale + lbias);
      }
      var out = new Int16Array(D * D * D * 3);
      for (p = 0; p < D; p++)
        for (q = 0; q < D; q++)
          for (r = 0; r < D; r++) {
            var R = gamma[p], G = gamma[q], B = gamma[r];
            var X = fr(fr(fr(R * S[0]) + fr(G * S[1])) + fr(B * S[2]));
            var Y = fr(fr(fr(R * S[3]) + fr(G * S[4])) + fr(B * S[5]));
            var Z = fr(fr(fr(R * S[6]) + fr(G * S[7])) + fr(B * S[8]));
            var FX = f(X), FY = f(Y), FZ = f(Z);
            var L = Y > lthresh ? fr(fr(116 * FY) - 16) : fr(f9033 * Y);
            var a = fr(500 * fr(FX - FY)), b = fr(200 * fr(FY - FZ));
            var idx = (p + q * D + r * D * D) * 3;
            out[idx] = cvRound(fr(fr(16384 * L) / 100));
            out[idx + 1] = cvRound(fr(fr(16384 * fr(a + 128)) / 256));
            out[idx + 2] = cvRound(fr(fr(16384 * fr(b + 128)) / 256));
          }
      return out;
    }
    // Lab (float32 values) of a BGR byte colour
    function labOf(b, g, r) {
      if (!labLut) labLut = buildLabLut();
      var D = 33, lut = labLut;
      var cx = cvRound(fr(b / 255) * 16384), cy = cvRound(fr(g / 255) * 16384), cz = cvRound(fr(r / 255) * 16384);
      var tx = cx >> 9, ty = cy >> 9, tz = cz >> 9, x = (cx >> 5) & 15, y = (cy >> 5) & 15, z = (cz >> 5) & 15;
      var a0 = 0, a1 = 0, a2 = 0;
      for (var i = 0; i < 8; i++) {
        var px = (i >> 2) & 1, qy = (i >> 1) & 1, rz = i & 1;
        var w = (px ? x : 16 - x) * (qy ? y : 16 - y) * (rz ? z : 16 - z);
        var idx = (Math.min(tx + px, D - 1) + Math.min(ty + qy, D - 1) * D + Math.min(tz + rz, D - 1) * D * D) * 3;
        a0 += lut[idx] * w;
        a1 += lut[idx + 1] * w;
        a2 += lut[idx + 2] * w;
      }
      return [fr(((a0 + 2048) >> 12) * 100 / 16384), fr(((a1 + 2048) >> 12) * 256 / 16384 - 128), fr(((a2 + 2048) >> 12) * 256 / 16384 - 128)];
    }
    // color_match_weight's 32x32x32 table (255 = fully matching), float32 like numpy
    var weightTables = {};
    function dropoutWeightTable(color, tolerance) {
      var key = color.join(",") + "/" + tolerance;
      if (weightTables[key]) return weightTables[key];
      var t = labOf(color[0], color[1], color[2]);
      var top = fr(DROPOUT_FALLOFF * tolerance), falloff = fr((DROPOUT_FALLOFF - 1.0) * tolerance);
      var table = new Uint8Array(32768);
      for (var bi = 0; bi < 32; bi++)
        for (var gi = 0; gi < 32; gi++)
          for (var ri = 0; ri < 32; ri++) {
            var lab = labOf(bi * 8 + 4, gi * 8 + 4, ri * 8 + 4);
            var d0 = fr(fr(lab[0] - t[0]) * LIGHTNESS_WEIGHT), d1 = fr(lab[1] - t[1]), d2 = fr(lab[2] - t[2]);
            var dist = fr(Math.sqrt(fr(fr(fr(d0 * d0) + fr(d1 * d1)) + fr(d2 * d2))));
            var wgt = clamp(fr(fr(top - dist) / falloff), 0, 1);
            table[(bi << 10) | (gi << 5) | ri] = roundHalfEven(fr(wgt * 255));
          }
      weightTables[key] = table;
      return table;
    }
    function pixelsToGray(px) {
      if (!px.channels || px.channels === 1) return makeImage(px.width, px.height, px.data);
      var n = px.width * px.height, s = px.channels, d = px.data, out = new Uint8Array(n);
      for (var i = 0, j = 0; i < n; i++, j += s) out[i] = (d[j] * 9798 + d[j + 1] * 19235 + d[j + 2] * 3735 + 16384) >> 15;
      return makeImage(px.width, px.height, out);
    }
    // apply_dropout: colour pixels -> the grey image the reader uses. `greyOf` (optional)
    // returns the plain grey conversion of px, so callers can share it.
    function applyDropout(px, spec, greyOf) {
      spec = normalizeDropout(spec);
      if (!px.channels || px.channels === 1) return makeImage(px.width, px.height, px.data);
      greyOf =
        greyOf ||
        function () {
          return pixelsToGray(px);
        };
      if (spec.mode === "grey") return greyOf();
      var n = px.width * px.height, s = px.channels, d = px.data, out = new Uint8Array(n), i, j;
      var needGrey = spec.mode === "color" || spec.strength < 1.0;
      var grey = needGrey ? greyOf().data : null;
      if (spec.mode === "red" || spec.mode === "green" || spec.mode === "blue") {
        var c = spec.mode === "red" ? 0 : spec.mode === "green" ? 1 : 2;
        for (i = 0, j = c; i < n; i++, j += s) out[i] = d[j];
      } else if (spec.mode === "max") {
        for (i = 0, j = 0; i < n; i++, j += s) {
          var m = d[j] > d[j + 1] ? d[j] : d[j + 1];
          out[i] = m > d[j + 2] ? m : d[j + 2];
        }
      } else {
        // Push matching pixels to white: grey + round((255 - grey) * w / 255)
        var table = dropoutWeightTable(spec.color, spec.tolerance), push = pushTable();
        for (i = 0, j = 0; i < n; i++, j += s) out[i] = push[(grey[i] << 8) | table[((d[j + 2] >> 3) << 10) | ((d[j + 1] >> 3) << 5) | (d[j] >> 3)]];
      }
      if (spec.strength < 1.0) {
        // cv2.addWeighted(grey, 1 - s, dropped, s, 0), tabulated over the byte pairs
        var blend = blendTable(spec.strength);
        for (i = 0; i < n; i++) out[i] = blend[(grey[i] << 8) | out[i]];
      }
      return makeImage(px.width, px.height, out);
    }
    // grey + round((255 - grey) * weight / 255), indexed (grey << 8) | weight
    var pushLut = null;
    function pushTable() {
      if (!pushLut) {
        pushLut = new Uint8Array(65536);
        for (var g = 0; g < 256; g++)
          for (var w = 0; w < 256; w++) {
            var v = g + roundHalfEven(((255 - g) * w) / 255);
            pushLut[(g << 8) | w] = v > 255 ? 255 : v;
          }
      }
      return pushLut;
    }
    // addWeighted(grey, 1 - s, dropped, s, 0) for every byte pair, indexed (grey << 8) | dropped
    var blendLuts = {};
    function blendTable(strength) {
      if (blendLuts[strength]) return blendLuts[strength];
      var al = fr(1.0 - strength), be = fr(strength), lut = new Uint8Array(65536);
      for (var g = 0; g < 256; g++)
        for (var v = 0; v < 256; v++) {
          var r = roundHalfEven(fr(g * al + fr(v * be)));
          lut[(g << 8) | v] = r < 0 ? 0 : r > 255 ? 255 : r;
        }
      blendLuts[strength] = lut;
      return lut;
    }

    function minMax(img) {
      var d = img.data, lo = 255, hi = 0;
      for (var i = 0, n = d.length; i < n; i++) {
        var v = d[i];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
      return [lo, hi];
    }

    // cv2.normalize(img, 0, 255, NORM_MINMAX) for uint8 images
    function normalizeMinMax(img) {
      var mm = minMax(img);
      var lo = mm[0], hi = mm[1];
      var out = new Uint8Array(img.data.length);
      if (hi - lo <= 0) {
        out.fill(0);
        return makeImage(img.width, img.height, out);
      }
      var scale = 255 / (hi - lo), shift = -lo * scale;
      var lut = new Uint8Array(256);
      for (var v = 0; v < 256; v++) lut[v] = clamp(cvRound(Math.fround(Math.fround(v * Math.fround(scale)) + Math.fround(shift))), 0, 255);
      var d = img.data;
      for (var i = 0, n = d.length; i < n; i++) out[i] = lut[d[i]];
      return makeImage(img.width, img.height, out);
    }

    function applyLut(img, lut) {
      var out = new Uint8Array(img.data.length), d = img.data;
      for (var i = 0, n = d.length; i < n; i++) out[i] = lut[d[i]];
      return makeImage(img.width, img.height, out);
    }

    function cropImage(img, x, y, w, h) {
      var x0 = clamp(x, 0, img.width), y0 = clamp(y, 0, img.height);
      var x1 = clamp(x + w, 0, img.width), y1 = clamp(y + h, 0, img.height);
      var cw = Math.max(0, x1 - x0), ch = Math.max(0, y1 - y0);
      var out = new Uint8Array(cw * ch);
      for (var r = 0; r < ch; r++) out.set(img.data.subarray((y0 + r) * img.width + x0, (y0 + r) * img.width + x0 + cw), r * cw);
      return makeImage(cw, ch, out);
    }

    // --- resize (cv2.resize INTER_LINEAR / INTER_AREA semantics) -------------
    function linearTab(ssize, dsize, scale, areaMode) {
      var xofs = new Int32Array(dsize), alpha = new Float32Array(dsize);
      var invScale = 1 / scale;
      for (var dx = 0; dx < dsize; dx++) {
        var fx, sx;
        if (!areaMode) {
          fx = Math.fround((dx + 0.5) * scale - 0.5);
          sx = Math.floor(fx);
          fx -= sx;
        } else {
          sx = Math.floor(dx * scale);
          fx = Math.fround(dx + 1 - (sx + 1) * invScale);
          fx = fx <= 0 ? 0 : fx - Math.floor(fx);
        }
        if (sx < 0) {
          fx = 0;
          sx = 0;
        }
        if (sx >= ssize - 1) {
          fx = 0;
          sx = ssize - 1;
        }
        xofs[dx] = sx;
        alpha[dx] = fx;
      }
      return { ofs: xofs, alpha: alpha };
    }

    function resizeLinear(img, dw, dh, areaUpsample) {
      var sw = img.width, sh = img.height;
      if (sw === dw && sh === dh) return cloneImage(img);
      var xt = linearTab(sw, dw, sw / dw, areaUpsample), yt = linearTab(sh, dh, sh / dh, areaUpsample);
      var src = img.data, out = new Uint8Array(dw * dh);
      // Fixed point like OpenCV (INTER_RESIZE_COEF_BITS = 11)
      var ax0 = new Int32Array(dw), ax1 = new Int32Array(dw), xs0 = xt.ofs, xs1 = new Int32Array(dw);
      for (var dx = 0; dx < dw; dx++) {
        var a1 = cvRound(xt.alpha[dx] * 2048);
        ax1[dx] = a1;
        ax0[dx] = 2048 - a1;
        xs1[dx] = Math.min(xs0[dx] + 1, sw - 1);
      }
      var row0 = new Int32Array(dw), row1 = new Int32Array(dw);
      var prev0 = -1, prev1 = -1;
      function hrow(sy, buf) {
        var base = sy * sw;
        for (var x = 0; x < dw; x++) buf[x] = src[base + xs0[x]] * ax0[x] + src[base + xs1[x]] * ax1[x];
      }
      for (var dy = 0; dy < dh; dy++) {
        var sy0 = yt.ofs[dy], sy1 = Math.min(sy0 + 1, sh - 1);
        var b1 = cvRound(yt.alpha[dy] * 2048), b0 = 2048 - b1;
        if (sy0 !== prev0) {
          if (sy0 === prev1) {
            var t = row0;
            row0 = row1;
            row1 = t;
          } else hrow(sy0, row0);
          prev0 = sy0;
        }
        if (sy1 !== prev1 || prev1 === -1 || row1 === row0) {
          hrow(sy1, row1);
          prev1 = sy1;
        }
        var o = dy * dw;
        for (var x = 0; x < dw; x++) {
          var v = (((b0 * (row0[x] >> 4)) >> 16) + ((b1 * (row1[x] >> 4)) >> 16) + 2) >> 2;
          out[o + x] = v < 0 ? 0 : v > 255 ? 255 : v;
        }
      }
      return makeImage(dw, dh, out);
    }

    function areaTab(ssize, dsize, scale) {
      var tab = [];
      for (var dx = 0; dx < dsize; dx++) {
        var fsx1 = dx * scale, fsx2 = fsx1 + scale;
        var cellWidth = Math.min(scale, ssize - fsx1);
        var sx1 = Math.ceil(fsx1), sx2 = Math.floor(fsx2);
        sx2 = Math.min(sx2, ssize - 1);
        sx1 = Math.min(sx1, sx2);
        if (sx1 - fsx1 > 1e-3) tab.push([dx, sx1 - 1, Math.fround((sx1 - fsx1) / cellWidth)]);
        for (var sx = sx1; sx < sx2; sx++) tab.push([dx, sx, Math.fround(1.0 / cellWidth)]);
        if (fsx2 - sx2 > 1e-3) tab.push([dx, sx2, Math.fround(Math.min(Math.min(fsx2 - sx2, 1), cellWidth) / cellWidth)]);
      }
      return tab;
    }

    // cv2.resize(..., interpolation=INTER_AREA); fx/fy given -> scale = 1/fx like OpenCV
    function resizeArea(img, dw, dh, invScaleX, invScaleY) {
      var sw = img.width, sh = img.height;
      if (sw === dw && sh === dh) return cloneImage(img);
      var scaleX = invScaleX ? 1 / invScaleX : sw / dw, scaleY = invScaleY ? 1 / invScaleY : sh / dh;
      if (scaleX < 1 || scaleY < 1) return resizeLinear(img, dw, dh, true);
      var src = img.data, out = new Uint8Array(dw * dh);
      var ix = Math.round(scaleX), iy = Math.round(scaleY);
      if (Math.abs(scaleX - ix) < 2.220446049250313e-16 && Math.abs(scaleY - iy) < 2.220446049250313e-16) {
        // integer decimation: plain block average
        var area = ix * iy;
        for (var dy = 0; dy < dh; dy++)
          for (var dx = 0; dx < dw; dx++) {
            var s = 0;
            for (var yy = 0; yy < iy; yy++) {
              var sy = Math.min(dy * iy + yy, sh - 1), b = sy * sw;
              for (var xx = 0; xx < ix; xx++) s += src[b + Math.min(dx * ix + xx, sw - 1)];
            }
            out[dy * dw + dx] = Math.floor((s + (area >> 1)) / area);
          }
        return makeImage(dw, dh, out);
      }
      var xtab = areaTab(sw, dw, scaleX), ytab = areaTab(sh, dh, scaleY);
      var n = xtab.length;
      var xstart = new Int32Array(dw + 1), xsi = new Int32Array(n), xal = new Float32Array(n);
      for (var k = 0; k < n; k++) {
        xsi[k] = xtab[k][1];
        xal[k] = xtab[k][2];
        xstart[xtab[k][0] + 1] = k + 1;
      }
      for (var q = 1; q <= dw; q++) if (xstart[q] < xstart[q - 1]) xstart[q] = xstart[q - 1];
      var buf = new Float32Array(dw), sum = new Float32Array(dw);
      var prevDy = ytab.length ? ytab[0][0] : 0, bufRow = -1;
      for (var j = 0; j < ytab.length; j++) {
        var ddy = ytab[j][0], ssy = ytab[j][1], beta = ytab[j][2];
        if (ssy !== bufRow) {
          var base = ssy * sw;
          for (var bx = 0; bx < dw; bx++) {
            var acc = 0;
            for (var k2 = xstart[bx], ke = xstart[bx + 1]; k2 < ke; k2++) acc += src[base + xsi[k2]] * xal[k2];
            buf[bx] = acc;
          }
          bufRow = ssy;
        }
        if (ddy !== prevDy) {
          for (var x = 0; x < dw; x++) {
            var sv = sum[x], r = Math.round(sv);
            if (r - sv === 0.5 && r & 1) r--;
            out[prevDy * dw + x] = r > 255 ? 255 : r;
            sum[x] = beta * buf[x];
          }
          prevDy = ddy;
        } else {
          for (var x2 = 0; x2 < dw; x2++) sum[x2] += beta * buf[x2];
        }
      }
      for (var x3 = 0; x3 < dw; x3++) out[prevDy * dw + x3] = clamp(cvRound(sum[x3]), 0, 255);
      return makeImage(dw, dh, out);
    }

    // --- filters -------------------------------------------------------------
    function reflect101(i, n) {
      if (n === 1) return 0;
      while (i < 0 || i >= n) {
        if (i < 0) i = -i;
        if (i >= n) i = 2 * n - 2 - i;
      }
      return i;
    }
    function gaussianKernel(ksize, sigma) {
      var small = { 1: [1], 3: [0.25, 0.5, 0.25], 5: [0.0625, 0.25, 0.375, 0.25, 0.0625], 7: [0.03125, 0.109375, 0.21875, 0.28125, 0.21875, 0.109375, 0.03125] };
      if (sigma <= 0 && small[ksize]) return small[ksize].slice();
      if (sigma <= 0) sigma = 0.3 * ((ksize - 1) * 0.5 - 1) + 0.8;
      var k = [], s = 0, c = (ksize - 1) / 2;
      for (var i = 0; i < ksize; i++) {
        var v = Math.exp(-((i - c) * (i - c)) / (2 * sigma * sigma));
        k.push(v);
        s += v;
      }
      return k.map(function (v) {
        return v / s;
      });
    }
    function sepFilter(img, kx, ky) {
      var w = img.width, h = img.height, src = img.data;
      var tmp = new Float32Array(w * h), out = new Uint8Array(w * h);
      var nx = kx.length, ny = ky.length, rx = (nx - 1) >> 1, ry = (ny - 1) >> 1;
      var x, y, k, s, b;
      // horizontal pass: interior without border checks
      for (y = 0; y < h; y++) {
        b = y * w;
        for (x = 0; x < w; x++) {
          s = 0;
          if (x >= rx && x < w - rx) for (k = 0; k < nx; k++) s += kx[k] * src[b + x + k - rx];
          else for (k = 0; k < nx; k++) s += kx[k] * src[b + reflect101(x + k - rx, w)];
          tmp[b + x] = s;
        }
      }
      var rows = new Int32Array(ny), acc = new Float32Array(w);
      for (y = 0; y < h; y++) {
        for (k = 0; k < ny; k++) rows[k] = reflect101(y + k - ry, h) * w;
        acc.fill(0);
        for (k = 0; k < ny; k++) {
          var kk = ky[k], r = rows[k];
          for (x = 0; x < w; x++) acc[x] += kk * tmp[r + x];
        }
        b = y * w;
        for (x = 0; x < w; x++) {
          var v = Math.round(acc[x]);
          out[b + x] = v < 0 ? 0 : v > 255 ? 255 : v;
        }
      }
      return makeImage(w, h, out);
    }
    // Exact integer path for OpenCV's fixed 3x3 / 5x5 kernels (sigma 0)
    function binomialBlur(img, taps) {
      var w = img.width, h = img.height, src = img.data, n = taps.length, r = n >> 1, norm = 0;
      for (var t = 0; t < n; t++) norm += taps[t];
      var tmp = new Int32Array(w * h), out = new Uint8Array(w * h), x, y, k, s, b;
      for (y = 0; y < h; y++) {
        b = y * w;
        for (x = 0; x < w; x++) {
          s = 0;
          if (x >= r && x < w - r) for (k = 0; k < n; k++) s += taps[k] * src[b + x + k - r];
          else for (k = 0; k < n; k++) s += taps[k] * src[b + reflect101(x + k - r, w)];
          tmp[b + x] = s;
        }
      }
      var rows = new Int32Array(n), acc = new Int32Array(w), total = norm * norm, half = total >> 1;
      for (y = 0; y < h; y++) {
        for (k = 0; k < n; k++) rows[k] = reflect101(y + k - r, h) * w;
        acc.fill(0);
        for (k = 0; k < n; k++) {
          var tk = taps[k], ro = rows[k];
          for (x = 0; x < w; x++) acc[x] += tk * tmp[ro + x];
        }
        b = y * w;
        for (x = 0; x < w; x++) out[b + x] = ((acc[x] + half) / total) | 0;
      }
      return makeImage(w, h, out);
    }
    function gaussianBlur(img, kw, kh, sigma) {
      if (!sigma && kw === kh && (kw === 3 || kw === 5)) return binomialBlur(img, kw === 3 ? [1, 2, 1] : [1, 4, 6, 4, 1]);
      return sepFilter(img, gaussianKernel(kw, sigma || 0), gaussianKernel(kh || kw, sigma || 0));
    }
    function medianBlur(img, ksize) {
      var w = img.width, h = img.height, src = img.data, out = new Uint8Array(w * h);
      var r = (ksize - 1) >> 1, hist = new Int32Array(256), half = (ksize * ksize) >> 1;
      for (var y = 0; y < h; y++) {
        hist.fill(0);
        for (var dy = -r; dy <= r; dy++) {
          var sy = clamp(y + dy, 0, h - 1);
          for (var dx = -r; dx <= r; dx++) hist[src[sy * w + clamp(dx, 0, w - 1)]]++;
        }
        for (var x = 0; x < w; x++) {
          if (x > 0) {
            var xo = clamp(x - r - 1, 0, w - 1), xn = clamp(x + r, 0, w - 1);
            for (var dy2 = -r; dy2 <= r; dy2++) {
              var sy2 = clamp(y + dy2, 0, h - 1) * w;
              hist[src[sy2 + xo]]--;
              hist[src[sy2 + xn]]++;
            }
          }
          var c = 0, v = 0;
          for (; v < 256; v++) {
            c += hist[v];
            if (c > half) break;
          }
          out[y * w + x] = v;
        }
      }
      return makeImage(w, h, out);
    }
    // Row-wise running min/max (van Herk / Gil-Werman, 3 comparisons per pixel):
    // window [x - lo, x + hi], pixels outside the row ignored
    function rowExtreme(src, w, h, lo, hi, isMax) {
      var k = lo + hi + 1, pad = isMax ? -1 : 256;
      var len = Math.ceil((w + lo + hi) / k) * k;
      var P = new Int16Array(len), g = new Int16Array(len), hs = new Int16Array(len), out = new Uint8Array(w * h);
      var i, start, end, e, v, y, row;
      for (y = 0; y < h; y++) {
        row = y * w;
        P.fill(pad);
        for (i = 0; i < w; i++) P[lo + i] = src[row + i];
        if (isMax) {
          for (start = 0; start < len; start += k) {
            end = start + k;
            e = pad;
            for (i = start; i < end; i++) {
              v = P[i];
              if (v > e) e = v;
              g[i] = e;
            }
            e = pad;
            for (i = end - 1; i >= start; i--) {
              v = P[i];
              if (v > e) e = v;
              hs[i] = e;
            }
          }
          for (i = 0; i < w; i++) {
            var a = hs[i], b = g[i + k - 1];
            out[row + i] = a > b ? a : b;
          }
        } else {
          for (start = 0; start < len; start += k) {
            end = start + k;
            e = pad;
            for (i = start; i < end; i++) {
              v = P[i];
              if (v < e) e = v;
              g[i] = e;
            }
            e = pad;
            for (i = end - 1; i >= start; i--) {
              v = P[i];
              if (v < e) e = v;
              hs[i] = e;
            }
          }
          for (i = 0; i < w; i++) {
            var a2 = hs[i], b2 = g[i + k - 1];
            out[row + i] = a2 < b2 ? a2 : b2;
          }
        }
      }
      return out;
    }
    function transpose(src, w, h) {
      var out = new Uint8Array(w * h), B = 32;
      for (var y0 = 0; y0 < h; y0 += B)
        for (var x0 = 0; x0 < w; x0 += B) {
          var y1 = Math.min(y0 + B, h), x1 = Math.min(x0 + B, w);
          for (var y = y0; y < y1; y++) {
            var r = y * w;
            for (var x = x0; x < x1; x++) out[x * h + y] = src[r + x];
          }
        }
      return out;
    }
    // min (erode) / max (dilate) over a rectangle [x-ax, x+bx] x [y-ay, y+by]; outside pixels ignored
    function rankFilterAsym(img, ax, bx, ay, by, isMax) {
      var w = img.width, h = img.height;
      var horiz = rowExtreme(img.data, w, h, ax, bx, isMax);
      var vert = rowExtreme(transpose(horiz, w, h), h, w, ay, by, isMax);
      return makeImage(w, h, transpose(vert, h, w));
    }
    function rankFilter(img, rx, ry, isMax) {
      return rankFilterAsym(img, rx, rx, ry, ry, isMax);
    }
    // Morphology with a rectangular kernel anchored at its centre like OpenCV:
    // covers [x - k/2, x + (k - 1 - k/2)]
    function morphRect(img, kw, kh, isMax) {
      var ax = kw >> 1, ay = kh >> 1;
      return rankFilterAsym(img, ax, kw - 1 - ax, ay, kh - 1 - ay, isMax);
    }
    // Binary (0/255) morphology with running counts: O(1) per pixel. Pixels outside
    // the image are ignored, like OpenCV's default morphology border.
    function morphBinary(img, kw, kh, isMax) {
      var w = img.width, h = img.height, src = img.data;
      var ax = kw >> 1, ay = kh >> 1, bx = kw - 1 - ax, by = kh - 1 - ay;
      var tmp = new Uint8Array(w * h), out = new Uint8Array(w * h), pre = new Int32Array(Math.max(w, h) + 1);
      var x, y, lo, hi, c;
      for (y = 0; y < h; y++) {
        var b = y * w;
        for (x = 0; x < w; x++) pre[x + 1] = pre[x] + (src[b + x] ? 1 : 0);
        for (x = 0; x < w; x++) {
          lo = x - ax < 0 ? 0 : x - ax;
          hi = x + bx >= w ? w - 1 : x + bx;
          c = pre[hi + 1] - pre[lo];
          tmp[b + x] = isMax ? (c > 0 ? 255 : 0) : c === hi - lo + 1 ? 255 : 0;
        }
      }
      for (x = 0; x < w; x++) {
        for (y = 0; y < h; y++) pre[y + 1] = pre[y] + (tmp[y * w + x] ? 1 : 0);
        for (y = 0; y < h; y++) {
          lo = y - ay < 0 ? 0 : y - ay;
          hi = y + by >= h ? h - 1 : y + by;
          c = pre[hi + 1] - pre[lo];
          out[y * w + x] = isMax ? (c > 0 ? 255 : 0) : c === hi - lo + 1 ? 255 : 0;
        }
      }
      return makeImage(w, h, out);
    }
    function morphClose(img, kw, kh) {
      return morphBinary(morphBinary(img, kw, kh, true), kw, kh, false);
    }

    // Otsu threshold value of a uint8 image (cv2.THRESH_OTSU)
    function otsuValue(img) {
      var hist = new Float64Array(256), d = img.data, n = d.length;
      for (var i = 0; i < n; i++) hist[d[i]]++;
      var mu = 0, scale = 1 / n;
      for (i = 0; i < 256; i++) mu += i * hist[i];
      mu *= scale;
      var mu1 = 0, q1 = 0, maxSigma = 0, maxVal = 0;
      for (i = 0; i < 256; i++) {
        var pi = hist[i] * scale;
        mu1 *= q1;
        q1 += pi;
        var q2 = 1 - q1;
        if (Math.min(q1, q2) < 1.1920929e-7 || Math.max(q1, q2) > 1 - 1.1920929e-7) continue;
        mu1 = (mu1 + i * pi) / q1;
        var mu2 = (mu - q1 * mu1) / q2;
        var sigma = q1 * q2 * (mu1 - mu2) * (mu1 - mu2);
        if (sigma > maxSigma) {
          maxSigma = sigma;
          maxVal = i;
        }
      }
      return maxVal;
    }
    function thresholdBinary(img, t, inverse) {
      var d = img.data, out = new Uint8Array(d.length);
      for (var i = 0, n = d.length; i < n; i++) out[i] = d[i] > t ? (inverse ? 0 : 255) : inverse ? 255 : 0;
      return makeImage(img.width, img.height, out);
    }

    // cv2.adaptiveThreshold(MEAN_C, THRESH_BINARY_INV, block, C)
    function adaptiveMeanThresholdInv(img, block, C) {
      var w = img.width, h = img.height, src = img.data, r = block >> 1;
      var out = new Uint8Array(w * h);
      // column sums with replicate border, sliding window
      var colSum = new Int32Array(w);
      var area = block * block, scale = 1 / area, idelta = Math.floor(C);
      for (var x = 0; x < w; x++) {
        var s = 0;
        for (var k = -r; k <= r; k++) s += src[clamp(k, 0, h - 1) * w + x];
        colSum[x] = s;
      }
      var xi = new Int32Array(w + 2 * r + 1);
      for (var y = 0; y < h; y++) {
        if (y > 0) {
          var ro = clamp(y - r - 1, 0, h - 1) * w, rn = clamp(y + r, 0, h - 1) * w;
          for (var x1 = 0; x1 < w; x1++) colSum[x1] += src[rn + x1] - src[ro + x1];
        }
        var rs = 0;
        for (var k2 = -r; k2 <= r; k2++) rs += colSum[clamp(k2, 0, w - 1)];
        var b = y * w;
        for (var x2 = 0; x2 < w; x2++) {
          if (x2 > 0) rs += colSum[Math.min(x2 + r, w - 1)] - colSum[Math.max(x2 - r - 1, 0)];
          var mean = (rs * scale + 0.5) | 0; // ties impossible: block area is odd
          out[b + x2] = src[b + x2] - mean <= -idelta ? 255 : 0;
        }
      }
      void xi;
      return makeImage(w, h, out);
    }

    // 8-connected components of non-zero pixels with stats (like
    // cv2.connectedComponentsWithStats), computed on horizontal runs so the cost is
    // proportional to the number of runs rather than pixels. Returns the runs too.
    function connectedComponents(bin) {
      var w = bin.width, h = bin.height, d = bin.data;
      var cap = 1 << 14, n = 0;
      var rx0 = new Int32Array(cap), rx1 = new Int32Array(cap), ry = new Int32Array(cap), rl = new Int32Array(cap);
      var parent = new Int32Array(cap + 1);
      var next = 1, prevStart = 0, prevEnd = 0;
      function grow() {
        cap *= 2;
        var a1 = new Int32Array(cap), a2 = new Int32Array(cap), a3 = new Int32Array(cap), a4 = new Int32Array(cap), a5 = new Int32Array(cap + 1);
        a1.set(rx0);
        a2.set(rx1);
        a3.set(ry);
        a4.set(rl);
        a5.set(parent);
        rx0 = a1;
        rx1 = a2;
        ry = a3;
        rl = a4;
        parent = a5;
      }
      for (var y = 0; y < h; y++) {
        var row = y * w, x = 0, curStart = n, p = prevStart;
        while (x < w) {
          while (x < w && !d[row + x]) x++;
          if (x >= w) break;
          var x0 = x;
          while (x < w && d[row + x]) x++;
          var x1 = x - 1;
          if (n >= cap) grow();
          rx0[n] = x0;
          rx1[n] = x1;
          ry[n] = y;
          var lab = 0;
          // previous-row runs overlapping [x0-1, x1+1]
          while (p < prevEnd && rx1[p] < x0 - 1) p++;
          for (var q = p; q < prevEnd && rx0[q] <= x1 + 1; q++) {
            var b = rl[q];
            while (parent[b] !== b) b = parent[b];
            if (!lab) lab = b;
            else if (b !== lab) {
              if (b < lab) {
                parent[lab] = b;
                lab = b;
              } else parent[b] = lab;
            }
          }
          if (!lab) {
            lab = next++;
            if (lab >= parent.length) grow();
            parent[lab] = lab;
          }
          rl[n] = lab;
          n++;
        }
        prevStart = curStart;
        prevEnd = n;
      }
      var remap = new Int32Array(next), count = 0;
      for (var k = 1; k < next; k++) {
        var t = k;
        while (parent[t] !== t) t = parent[t];
        remap[k] = t === k ? ++count : remap[t];
      }
      var area = new Float64Array(count + 1), sx = new Float64Array(count + 1), sy = new Float64Array(count + 1);
      var minx = new Int32Array(count + 1).fill(w), miny = new Int32Array(count + 1).fill(h), maxx = new Int32Array(count + 1).fill(-1), maxy = new Int32Array(count + 1).fill(-1);
      for (var i = 0; i < n; i++) {
        var L = remap[rl[i]], a0 = rx0[i], a1 = rx1[i], yy = ry[i], len = a1 - a0 + 1;
        rl[i] = L;
        area[L] += len;
        sx[L] += ((a0 + a1) * len) / 2;
        sy[L] += yy * len;
        if (a0 < minx[L]) minx[L] = a0;
        if (a1 > maxx[L]) maxx[L] = a1;
        if (yy < miny[L]) miny[L] = yy;
        if (yy > maxy[L]) maxy[L] = yy;
      }
      var comps = [];
      for (var c = 1; c <= count; c++) {
        comps.push({ label: c, area: area[c], cx: sx[c] / area[c], cy: sy[c] / area[c], x: minx[c], y: miny[c], w: maxx[c] - minx[c] + 1, h: maxy[c] - miny[c] + 1 });
      }
      return { components: comps, runs: { count: n, x0: rx0, x1: rx1, y: ry, label: rl } };
    }

    // --- perspective warp (cv2.warpPerspective INTER_LINEAR, WARP_INVERSE_MAP) --
    // OpenCV >= 4.11 interpolates 8-bit images in float without the old 1/32
    // coordinate quantisation; this matches it to within 1 grey level.
    function bilinearAt(s, sw, sh, X, Y, bv) {
      var sx = Math.floor(X), sy = Math.floor(Y);
      var ax = X - sx, ay = Y - sy, v;
      if (sx >= 0 && sy >= 0 && sx < sw - 1 && sy < sh - 1) {
        var p = sy * sw + sx;
        v = (s[p] * (1 - ax) + s[p + 1] * ax) * (1 - ay) + (s[p + sw] * (1 - ax) + s[p + sw + 1] * ax) * ay;
      } else if (sx >= sw || sx + 1 < 0 || sy >= sh || sy + 1 < 0 || X !== X || Y !== Y) {
        return bv;
      } else {
        var in0 = sy >= 0 && sy < sh, in1 = sy + 1 >= 0 && sy + 1 < sh, ix0 = sx >= 0 && sx < sw, ix1 = sx + 1 >= 0 && sx + 1 < sw;
        var v00 = in0 && ix0 ? s[sy * sw + sx] : bv, v01 = in0 && ix1 ? s[sy * sw + sx + 1] : bv;
        var v10 = in1 && ix0 ? s[(sy + 1) * sw + sx] : bv, v11 = in1 && ix1 ? s[(sy + 1) * sw + sx + 1] : bv;
        v = (v00 * (1 - ax) + v01 * ax) * (1 - ay) + (v10 * (1 - ax) + v11 * ax) * ay;
      }
      v = Math.round(v);
      return v < 0 ? 0 : v > 255 ? 255 : v;
    }

    // Sample src at H*(x, y) for every output pixel; H maps output -> source
    function warpPerspective(src, H, dw, dh, borderValue) {
      if (borderValue === undefined) borderValue = 255;
      var sw = src.width, sh = src.height, s = src.data;
      var out = new Uint8Array(dw * dh);
      var m0 = H[0], m1 = H[1], m2 = H[2], m3 = H[3], m4 = H[4], m5 = H[5], m6 = H[6], m7 = H[7], m8 = H[8];
      var bv = borderValue, swm = sw - 1, shm = sh - 1;
      for (var y = 0; y < dh; y++) {
        var X0 = m1 * y + m2, Y0 = m4 * y + m5, W0 = m7 * y + m8;
        var o = y * dw;
        for (var x = 0; x < dw; x++) {
          var W = W0 + m6 * x;
          W = W ? 1 / W : 0;
          var X = (X0 + m0 * x) * W, Y = (Y0 + m3 * x) * W;
          var sx = X | 0, sy = Y | 0;
          if (X >= 0 && Y >= 0 && sx < swm && sy < shm) {
            var ax = X - sx, ay = Y - sy, p = sy * sw + sx;
            var v = (s[p] * (1 - ax) + s[p + 1] * ax) * (1 - ay) + (s[p + sw] * (1 - ax) + s[p + sw + 1] * ax) * ay;
            out[o + x] = (v + 0.5) | 0;
          } else out[o + x] = bilinearAt(s, sw, sh, X, Y, bv);
        }
      }
      return makeImage(dw, dh, out);
    }

    // cv2.remap with float maps (INTER_LINEAR, constant border)
    function remapLinear(src, mapX, mapY, dw, dh, borderValue) {
      var out = new Uint8Array(dw * dh);
      for (var i = 0, n = dw * dh; i < n; i++) out[i] = bilinearAt(src.data, src.width, src.height, mapX[i], mapY[i], borderValue);
      return makeImage(dw, dh, out);
    }

    // ------------------------------------------------------------------------
    // Linear algebra and homographies (row-major 3x3 as length-9 arrays)
    // ------------------------------------------------------------------------
    function solveLinear(A, b) {
      // Gaussian elimination with partial pivoting; A is n arrays of n numbers
      var n = b.length, M = [], i, j, k;
      for (i = 0; i < n; i++) {
        M.push(A[i].slice());
        M[i].push(b[i]);
      }
      for (k = 0; k < n; k++) {
        var p = k, best = Math.abs(M[k][k]);
        for (i = k + 1; i < n; i++)
          if (Math.abs(M[i][k]) > best) {
            best = Math.abs(M[i][k]);
            p = i;
          }
        if (best < 1e-300) return null;
        if (p !== k) {
          var t = M[p];
          M[p] = M[k];
          M[k] = t;
        }
        for (i = k + 1; i < n; i++) {
          var f = M[i][k] / M[k][k];
          if (f === 0) continue;
          for (j = k; j <= n; j++) M[i][j] -= f * M[k][j];
        }
      }
      var x = new Array(n);
      for (i = n - 1; i >= 0; i--) {
        var s = M[i][n];
        for (j = i + 1; j < n; j++) s -= M[i][j] * x[j];
        x[i] = s / M[i][i];
      }
      return x;
    }

    // Eigen decomposition of a symmetric matrix (cyclic Jacobi). Returns {values, vectors(cols)}
    function jacobiEigen(S) {
      var n = S.length, a = S.map(function (r) {
        return r.slice();
      });
      var v = [];
      for (var i = 0; i < n; i++) {
        v.push(new Array(n).fill(0));
        v[i][i] = 1;
      }
      for (var sweep = 0; sweep < 60; sweep++) {
        var off = 0;
        for (var p = 0; p < n; p++) for (var q = p + 1; q < n; q++) off += a[p][q] * a[p][q];
        if (off < 1e-30) break;
        for (var p2 = 0; p2 < n; p2++)
          for (var q2 = p2 + 1; q2 < n; q2++) {
            if (Math.abs(a[p2][q2]) < 1e-300) continue;
            var theta = (a[q2][q2] - a[p2][p2]) / (2 * a[p2][q2]);
            var t = (theta >= 0 ? 1 : -1) / (Math.abs(theta) + Math.sqrt(theta * theta + 1));
            var c = 1 / Math.sqrt(t * t + 1), s = t * c;
            for (var k = 0; k < n; k++) {
              var akp = a[k][p2], akq = a[k][q2];
              a[k][p2] = c * akp - s * akq;
              a[k][q2] = s * akp + c * akq;
            }
            for (var k2 = 0; k2 < n; k2++) {
              var apk = a[p2][k2], aqk = a[q2][k2];
              a[p2][k2] = c * apk - s * aqk;
              a[q2][k2] = s * apk + c * aqk;
            }
            for (var k3 = 0; k3 < n; k3++) {
              var vkp = v[k3][p2], vkq = v[k3][q2];
              v[k3][p2] = c * vkp - s * vkq;
              v[k3][q2] = s * vkp + c * vkq;
            }
          }
      }
      var values = [];
      for (var d = 0; d < n; d++) values.push(a[d][d]);
      return { values: values, vectors: v };
    }

    function projectPoint(H, x, y) {
      var w = H[6] * x + H[7] * y + H[8];
      w = w ? 1 / w : 0;
      return [(H[0] * x + H[1] * y + H[2]) * w, (H[3] * x + H[4] * y + H[5]) * w];
    }
    function projectPoints(H, pts) {
      return pts.map(function (p) {
        return projectPoint(H, p[0], p[1]);
      });
    }
    function invert3x3(m) {
      var a = m[0], b = m[1], c = m[2], d = m[3], e = m[4], f = m[5], g = m[6], h = m[7], i = m[8];
      var A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
      var det = a * A + b * B + c * C;
      if (!det) return null;
      var inv = [A, -(b * i - c * h), b * f - c * e, B, a * i - c * g, -(a * f - c * d), C, -(a * h - b * g), a * e - b * d];
      for (var k = 0; k < 9; k++) inv[k] /= det;
      return inv;
    }
    function mul3x3(a, b) {
      var r = new Array(9);
      for (var i = 0; i < 3; i++) for (var j = 0; j < 3; j++) r[i * 3 + j] = a[i * 3] * b[j] + a[i * 3 + 1] * b[3 + j] + a[i * 3 + 2] * b[6 + j];
      return r;
    }
    // cv2.getPerspectiveTransform: exact 4-point solve
    function getPerspectiveTransform(src, dst) {
      var A = [], b = [];
      for (var i = 0; i < 4; i++) {
        var x = src[i][0], y = src[i][1], u = dst[i][0], v = dst[i][1];
        A.push([x, y, 1, 0, 0, 0, -x * u, -y * u]);
        b.push(u);
        A.push([0, 0, 0, x, y, 1, -x * v, -y * v]);
        b.push(v);
      }
      var h = solveLinear(A, b);
      if (!h) return null;
      return [h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1];
    }
    function normalizationMatrix(pts) {
      var n = pts.length, cx = 0, cy = 0, d = 0;
      for (var i = 0; i < n; i++) {
        cx += pts[i][0];
        cy += pts[i][1];
      }
      cx /= n;
      cy /= n;
      for (var j = 0; j < n; j++) d += Math.abs(pts[j][0] - cx) + Math.abs(pts[j][1] - cy);
      var s = d ? n / d : 1;
      return [s, 0, -cx * s, 0, s, -cy * s, 0, 0, 1];
    }
    // Normalised DLT (least squares over all points)
    function homographyDLT(src, dst) {
      var n = src.length;
      if (n < 4) return null;
      var T1 = normalizationMatrix(src), T2 = normalizationMatrix(dst);
      var ata = [];
      for (var r = 0; r < 9; r++) ata.push(new Array(9).fill(0));
      for (var i = 0; i < n; i++) {
        var x = T1[0] * src[i][0] + T1[2], y = T1[4] * src[i][1] + T1[5];
        var u = T2[0] * dst[i][0] + T2[2], v = T2[4] * dst[i][1] + T2[5];
        var r1 = [x, y, 1, 0, 0, 0, -u * x, -u * y, -u], r2 = [0, 0, 0, x, y, 1, -v * x, -v * y, -v];
        for (var a = 0; a < 9; a++)
          for (var b = a; b < 9; b++) ata[a][b] += r1[a] * r1[b] + r2[a] * r2[b];
      }
      for (var a2 = 0; a2 < 9; a2++) for (var b2 = 0; b2 < a2; b2++) ata[a2][b2] = ata[b2][a2];
      var eig = jacobiEigen(ata), mi = 0;
      for (var k = 1; k < 9; k++) if (eig.values[k] < eig.values[mi]) mi = k;
      var h = [];
      for (var k2 = 0; k2 < 9; k2++) h.push(eig.vectors[k2][mi]);
      var H = mul3x3(invert3x3(T2), mul3x3(h, T1));
      if (!H[8]) return null;
      for (var k3 = 0; k3 < 9; k3++) H[k3] /= H[8];
      return H;
    }
    // Levenberg-Marquardt refinement of the reprojection error (h22 fixed to 1)
    function refineHomography(H, src, dst, iterations) {
      var h = H.slice(0, 8), n = src.length, lambda = 1e-3;
      function cost(hh) {
        var e = 0;
        for (var i = 0; i < n; i++) {
          var x = src[i][0], y = src[i][1], w = hh[6] * x + hh[7] * y + 1;
          var dx = (hh[0] * x + hh[1] * y + hh[2]) / w - dst[i][0], dy = (hh[3] * x + hh[4] * y + hh[5]) / w - dst[i][1];
          e += dx * dx + dy * dy;
        }
        return e;
      }
      var current = cost(h);
      for (var it = 0; it < (iterations || 20); it++) {
        var JtJ = [], Jtr = new Array(8).fill(0);
        for (var r = 0; r < 8; r++) JtJ.push(new Array(8).fill(0));
        for (var i = 0; i < n; i++) {
          var x = src[i][0], y = src[i][1], w = h[6] * x + h[7] * y + 1, iw = 1 / w;
          var u = (h[0] * x + h[1] * y + h[2]) * iw, v = (h[3] * x + h[4] * y + h[5]) * iw;
          var ru = u - dst[i][0], rv = v - dst[i][1];
          var Ju = [x * iw, y * iw, iw, 0, 0, 0, -u * x * iw, -u * y * iw];
          var Jv = [0, 0, 0, x * iw, y * iw, iw, -v * x * iw, -v * y * iw];
          for (var a = 0; a < 8; a++) {
            Jtr[a] += Ju[a] * ru + Jv[a] * rv;
            for (var b = a; b < 8; b++) JtJ[a][b] += Ju[a] * Ju[b] + Jv[a] * Jv[b];
          }
        }
        for (var a2 = 0; a2 < 8; a2++) for (var b2 = 0; b2 < a2; b2++) JtJ[a2][b2] = JtJ[b2][a2];
        var improved = false;
        for (var tries = 0; tries < 10; tries++) {
          var M = JtJ.map(function (row, idx) {
            var rr = row.slice();
            rr[idx] *= 1 + lambda;
            return rr;
          });
          var step = solveLinear(
            M,
            Jtr.map(function (g) {
              return -g;
            })
          );
          if (!step) break;
          var cand = h.map(function (val, idx) {
            return val + step[idx];
          });
          var c = cost(cand);
          if (c < current) {
            h = cand;
            var gain = current - c;
            current = c;
            lambda = Math.max(lambda / 10, 1e-12);
            improved = true;
            if (gain < 1e-14 * (1 + current)) it = 1e9;
            break;
          }
          lambda *= 10;
        }
        if (!improved) break;
      }
      return h.concat([1]);
    }
    function findHomographyLS(src, dst) {
      var H = homographyDLT(src, dst);
      if (!H) return null;
      return refineHomography(H, src, dst, 20);
    }
    function reprojErrorSq(H, s, d) {
      var p = projectPoint(H, s[0], s[1]);
      var dx = p[0] - d[0], dy = p[1] - d[1];
      return dx * dx + dy * dy;
    }
    function collinear3(a, b, c) {
      return Math.abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) < 1e-6 * (1 + Math.abs(b[0] - a[0]) + Math.abs(c[1] - a[1]));
    }
    // RANSAC (deterministic seed) followed by LS + LM on the inliers
    function findHomographyRansac(src, dst, threshold, seed) {
      var n = src.length;
      if (n < 4) return null;
      if (n === 4) return { H: getPerspectiveTransform(src, dst), inliers: [0, 1, 2, 3] };
      var rand = mulberry32(seed || 12345), thr2 = threshold * threshold;
      var bestCount = -1, bestInliers = null, maxIters = 2000, iters = 0, conf = 0.995;
      while (iters < maxIters) {
        iters++;
        var idx = [];
        while (idx.length < 4) {
          var r = Math.floor(rand() * n);
          if (idx.indexOf(r) < 0) idx.push(r);
        }
        var s4 = idx.map(function (k) {
          return src[k];
        }),
          d4 = idx.map(function (k) {
            return dst[k];
          });
        if (collinear3(s4[0], s4[1], s4[2]) || collinear3(s4[0], s4[1], s4[3]) || collinear3(s4[0], s4[2], s4[3]) || collinear3(s4[1], s4[2], s4[3])) continue;
        var H = getPerspectiveTransform(s4, d4);
        if (!H) continue;
        var inl = [];
        for (var i = 0; i < n; i++) if (reprojErrorSq(H, src[i], dst[i]) <= thr2) inl.push(i);
        if (inl.length > bestCount) {
          bestCount = inl.length;
          bestInliers = inl;
          var ratio = inl.length / n;
          var denom = Math.log(1 - Math.pow(ratio, 4));
          if (denom < 0) maxIters = Math.min(maxIters, Math.ceil(Math.log(1 - conf) / denom));
          if (inl.length === n) break;
        }
      }
      if (!bestInliers || bestInliers.length < 4) return null;
      var si = bestInliers.map(function (k) {
        return src[k];
      }),
        di = bestInliers.map(function (k) {
          return dst[k];
        });
      var Hf = findHomographyLS(si, di);
      if (!Hf) return null;
      return { H: Hf, inliers: bestInliers };
    }

    // ------------------------------------------------------------------------
    // Geometry: convex hull, Douglas-Peucker, min-area rectangle, point order
    // ------------------------------------------------------------------------
    function convexHull(points) {
      var pts = points.slice().sort(function (a, b) {
        return a[0] - b[0] || a[1] - b[1];
      });
      if (pts.length < 3) return pts;
      function cross(o, a, b) {
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
      }
      var lower = [], upper = [];
      for (var i = 0; i < pts.length; i++) {
        while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], pts[i]) <= 0) lower.pop();
        lower.push(pts[i]);
      }
      for (var j = pts.length - 1; j >= 0; j--) {
        while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], pts[j]) <= 0) upper.pop();
        upper.push(pts[j]);
      }
      upper.pop();
      lower.pop();
      return lower.concat(upper);
    }
    function polygonArea(poly) {
      var a = 0;
      for (var i = 0; i < poly.length; i++) {
        var p = poly[i], q = poly[(i + 1) % poly.length];
        a += p[0] * q[1] - q[0] * p[1];
      }
      return Math.abs(a) / 2;
    }
    function perimeter(poly) {
      var s = 0;
      for (var i = 0; i < poly.length; i++) {
        var p = poly[i], q = poly[(i + 1) % poly.length];
        s += Math.hypot(q[0] - p[0], q[1] - p[1]);
      }
      return s;
    }
    function pointLineDist(p, a, b) {
      var dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy);
      if (!len) return Math.hypot(p[0] - a[0], p[1] - a[1]);
      return Math.abs(dy * (p[0] - a[0]) - dx * (p[1] - a[1])) / len;
    }
    function douglasPeucker(pts, eps) {
      if (pts.length < 3) return pts.slice();
      var keep = new Uint8Array(pts.length);
      keep[0] = keep[pts.length - 1] = 1;
      var stack = [[0, pts.length - 1]];
      while (stack.length) {
        var seg = stack.pop(), a = seg[0], b = seg[1], md = -1, mi = -1;
        for (var i = a + 1; i < b; i++) {
          var d = pointLineDist(pts[i], pts[a], pts[b]);
          if (d > md) {
            md = d;
            mi = i;
          }
        }
        if (md > eps) {
          keep[mi] = 1;
          stack.push([a, mi], [mi, b]);
        }
      }
      return pts.filter(function (_, i) {
        return keep[i];
      });
    }
    // Closed-polygon simplification (start from the two farthest-apart vertices)
    function approxPolyClosed(poly, eps) {
      if (poly.length <= 4) return poly.slice();
      var i0 = 0, i1 = 0, best = -1;
      for (var i = 0; i < poly.length; i++) {
        var d = Math.hypot(poly[i][0] - poly[0][0], poly[i][1] - poly[0][1]);
        if (d > best) {
          best = d;
          i1 = i;
        }
      }
      best = -1;
      for (var j = 0; j < poly.length; j++) {
        var d2 = Math.hypot(poly[j][0] - poly[i1][0], poly[j][1] - poly[i1][1]);
        if (d2 > best) {
          best = d2;
          i0 = j;
        }
      }
      var n = poly.length, a = [], b = [];
      for (var k = i0; ; k = (k + 1) % n) {
        a.push(poly[k]);
        if (k === i1) break;
      }
      for (var k2 = i1; ; k2 = (k2 + 1) % n) {
        b.push(poly[k2]);
        if (k2 === i0) break;
      }
      var ra = douglasPeucker(a, eps), rb = douglasPeucker(b, eps);
      return ra.slice(0, -1).concat(rb.slice(0, -1));
    }
    function minAreaRect(hull) {
      var best = null;
      for (var i = 0; i < hull.length; i++) {
        var p = hull[i], q = hull[(i + 1) % hull.length];
        var ex = q[0] - p[0], ey = q[1] - p[1], len = Math.hypot(ex, ey);
        if (!len) continue;
        ex /= len;
        ey /= len;
        var minU = Infinity, maxU = -Infinity, minV = Infinity, maxV = -Infinity;
        for (var k = 0; k < hull.length; k++) {
          var u = hull[k][0] * ex + hull[k][1] * ey, v = -hull[k][0] * ey + hull[k][1] * ex;
          if (u < minU) minU = u;
          if (u > maxU) maxU = u;
          if (v < minV) minV = v;
          if (v > maxV) maxV = v;
        }
        var area = (maxU - minU) * (maxV - minV);
        if (!best || area < best.area) best = { area: area, ex: ex, ey: ey, minU: minU, maxU: maxU, minV: minV, maxV: maxV };
      }
      if (!best) return hull.slice(0, 4);
      function pt(u, v) {
        return [u * best.ex - v * best.ey, u * best.ey + v * best.ex];
      }
      return [pt(best.minU, best.minV), pt(best.maxU, best.minV), pt(best.maxU, best.maxV), pt(best.minU, best.maxV)];
    }
    // ImageUtils.order_points: tl, tr, br, bl
    function orderPoints(pts) {
      var s = pts.map(function (p) {
        return p[0] + p[1];
      }),
        d = pts.map(function (p) {
          return p[1] - p[0];
        });
      function argmin(a) {
        var m = 0;
        for (var i = 1; i < a.length; i++) if (a[i] < a[m]) m = i;
        return m;
      }
      function argmax(a) {
        var m = 0;
        for (var i = 1; i < a.length; i++) if (a[i] > a[m]) m = i;
        return m;
      }
      return [pts[argmin(s)], pts[argmin(d)], pts[argmax(s)], pts[argmax(d)]].map(function (p) {
        return [p[0], p[1]];
      });
    }

    // Largest bright region (the page) of a binary image -> hull and quad
    function largestRegionQuad(bin, epsFactor) {
      var cc = connectedComponents(bin), comps = cc.components;
      if (!comps.length) return null;
      var best = comps[0];
      for (var i = 1; i < comps.length; i++) if (comps[i].area > best.area) best = comps[i];
      var runs = cc.runs, lo = new Int32Array(bin.height).fill(-1), hi = new Int32Array(bin.height).fill(-1), pts = [];
      for (var r = 0; r < runs.count; r++) {
        if (runs.label[r] !== best.label) continue;
        var yy = runs.y[r];
        if (lo[yy] < 0 || runs.x0[r] < lo[yy]) lo[yy] = runs.x0[r];
        if (runs.x1[r] > hi[yy]) hi[yy] = runs.x1[r];
      }
      for (var y = 0; y < bin.height; y++) {
        if (lo[y] < 0) continue;
        pts.push([lo[y], y]);
        if (hi[y] !== lo[y]) pts.push([hi[y], y]);
      }
      var hull = convexHull(pts);
      if (hull.length < 3) return null;
      var area = polygonArea(hull);
      var quad = approxPolyClosed(hull, epsFactor * perimeter(hull));
      var fromRect = false;
      if (quad.length !== 4) {
        quad = minAreaRect(hull);
        fromRect = true;
      }
      return { quad: quad, hull: hull, area: area, fromRect: fromRect };
    }

    // ------------------------------------------------------------------------
    // Ellipse mask exactly like cv2.ellipse(mask, c, axes, 0, 0, 360, 255, -1)
    // (ellipse2Poly + FillConvexPoly with LINE_8 edges, 16-bit fixed point)
    // ------------------------------------------------------------------------
    var XY_SHIFT = 16, XY_ONE = 65536;
    function sinDeg(a) {
      return Math.fround(Math.sin((a * Math.PI) / 180));
    }
    // cv::clipLine on the XY_SHIFT-scaled image size; null when the line is outside
    function clipLine(width, height, x1, y1, x2, y2) {
      var right = width - 1, bottom = height - 1, a;
      var c1 = (x1 < 0) + (x1 > right) * 2 + (y1 < 0) * 4 + (y1 > bottom) * 8;
      var c2 = (x2 < 0) + (x2 > right) * 2 + (y2 < 0) * 4 + (y2 > bottom) * 8;
      if ((c1 & c2) === 0 && (c1 | c2) !== 0) {
        if (c1 & 12) {
          a = c1 < 8 ? 0 : bottom;
          x1 += Math.trunc(((a - y1) * (x2 - x1)) / (y2 - y1));
          y1 = a;
          c1 = (x1 < 0) + (x1 > right) * 2;
        }
        if (c2 & 12) {
          a = c2 < 8 ? 0 : bottom;
          x2 += Math.trunc(((a - y2) * (x2 - x1)) / (y2 - y1));
          y2 = a;
          c2 = (x2 < 0) + (x2 > right) * 2;
        }
        if ((c1 & c2) === 0 && (c1 | c2) !== 0) {
          if (c1) {
            a = c1 === 1 ? 0 : right;
            y1 += Math.trunc(((a - x1) * (y2 - y1)) / (x2 - x1));
            x1 = a;
            c1 = 0;
          }
          if (c2) {
            a = c2 === 1 ? 0 : right;
            y2 += Math.trunc(((a - x2) * (y2 - y1)) / (x2 - x1));
            x2 = a;
            c2 = 0;
          }
        }
      }
      return (c1 | c2) === 0 ? [x1, y1, x2, y2] : null;
    }
    function line2(mask, w, h, p1x, p1y, p2x, p2y) {
      var clipped = clipLine(w * XY_ONE, h * XY_ONE, p1x, p1y, p2x, p2y);
      if (!clipped) return;
      p1x = clipped[0];
      p1y = clipped[1];
      p2x = clipped[2];
      p2y = clipped[3];
      var dx = p2x - p1x, dy = p2y - p1y;
      var ax = Math.abs(dx), ay = Math.abs(dy), t, xStep, yStep, ecount;
      if (ax > ay) {
        if (dx < 0) {
          t = p1x; p1x = p2x; p2x = t;
          t = p1y; p1y = p2y; p2y = t;
          dy = -dy;
        }
        xStep = XY_ONE;
        yStep = Math.trunc((dy * XY_ONE) / (ax | 1));
        ecount = floorDiv(p2x - p1x, XY_SHIFT);
      } else {
        if (dy < 0) {
          t = p1x; p1x = p2x; p2x = t;
          t = p1y; p1y = p2y; p2y = t;
          dx = -dx;
        }
        xStep = Math.trunc((dx * XY_ONE) / (ay | 1));
        yStep = XY_ONE;
        ecount = floorDiv(p2y - p1y, XY_SHIFT);
      }
      p1x += XY_ONE >> 1;
      p1y += XY_ONE >> 1;
      function put(x, y) {
        if (x >= 0 && x < w && y >= 0 && y < h) mask[y * w + x] = 1;
      }
      put(floorDiv(p2x + (XY_ONE >> 1), XY_SHIFT), floorDiv(p2y + (XY_ONE >> 1), XY_SHIFT));
      if (ax > ay) {
        p1x = floorDiv(p1x, XY_SHIFT);
        while (ecount >= 0) {
          put(p1x, floorDiv(p1y, XY_SHIFT));
          p1x++;
          p1y += yStep;
          ecount--;
        }
      } else {
        p1y = floorDiv(p1y, XY_SHIFT);
        while (ecount >= 0) {
          put(floorDiv(p1x, XY_SHIFT), p1y);
          p1x += xStep;
          p1y++;
          ecount--;
        }
      }
    }
    function ellipseMask(w, h, cx, cy, axW, axH) {
      var mask = new Uint8Array(w * h);
      fillConvexPoly(mask, w, h, ellipsePoly(cx, cy, axW, axH));
      return mask;
    }
    // cv2.ellipse(mask, c, axes, 0, 0, 360, 1, thickness 2): LINE_8 thick polyline
    function ellipseOutline(mask, w, h, cx, cy, axW, axH) {
      var v = ellipsePoly(cx, cy, axW, axH), flags = 3, p0 = v[0];
      for (var i = 1; i < v.length; i++) {
        thickLine2(mask, w, h, p0, v[i], flags);
        p0 = v[i];
        flags = 2;
      }
    }
    // ThickLine for thickness 2 with XY_SHIFT fixed-point ends
    function thickLine2(mask, w, h, p0, p1, flags) {
      var dx = (p0[0] - p1[0]) / XY_ONE, dy = (p1[1] - p0[1]) / XY_ONE, r = dx * dx + dy * dy, th = 2 << (XY_SHIFT - 1);
      if (Math.abs(r) > 2.220446049250313e-16) {
        r = th / Math.sqrt(r);
        var ex = cvRound(dy * r), ey = cvRound(dx * r);
        fillConvexPoly(mask, w, h, [[p0[0] + ex, p0[1] + ey], [p0[0] - ex, p0[1] - ey], [p1[0] - ex, p1[1] - ey], [p1[0] + ex, p1[1] + ey]]);
      }
      var c = p0;
      for (var i = 0; i < 2; i++) {
        if (flags & (i + 1)) filledCircle(mask, w, h, floorDiv(c[0] + (XY_ONE >> 1), XY_SHIFT), floorDiv(c[1] + (XY_ONE >> 1), XY_SHIFT), (th + (XY_ONE >> 1)) >> XY_SHIFT);
        c = p1;
      }
    }
    // OpenCV Circle(..., fill=1)
    function filledCircle(mask, w, h, cx, cy, radius) {
      var err = 0, dx = radius, dy = 0, plus = 1, minus = (radius << 1) - 1;
      function hline(y, x1, x2) {
        if (y < 0 || y >= h) return;
        x1 = Math.max(x1, 0);
        x2 = Math.min(x2, w - 1);
        for (var x = x1; x <= x2; x++) mask[y * w + x] = 1;
      }
      while (dx >= dy) {
        var y11 = cy - dy, y12 = cy + dy, y21 = cy - dx, y22 = cy + dx;
        var x11 = cx - dx, x12 = cx + dx, x21 = cx - dy, x22 = cx + dy;
        if (x11 < w && x12 >= 0 && y21 < h && y22 >= 0) {
          hline(y11, x11, x12);
          hline(y12, x11, x12);
          if (x21 < w && x22 >= 0) {
            hline(y21, x21, x22);
            hline(y22, x21, x22);
          }
        }
        dy++;
        err += plus;
        plus += 2;
        var m = (err <= 0 ? 1 : 0) - 1;
        err -= minus & m;
        dx += m;
        minus -= m & 2;
      }
    }
    // ellipse2Poly vertices in XY_SHIFT fixed point (EllipseEx)
    function ellipsePoly(cx, cy, axW, axH) {
      var cX = cx * XY_ONE, cY = cy * XY_ONE, aW = axW * XY_ONE, aH = axH * XY_ONE;
      var delta = floorDiv(Math.max(aW, aH) + (XY_ONE >> 1), XY_SHIFT);
      delta = delta < 3 ? 90 : delta < 10 ? 30 : delta < 15 ? 18 : 5;
      var v = [], prev = null;
      for (var i = 0; i < 360 + delta; i += delta) {
        var ang = i > 360 ? 360 : i;
        var x = aW * sinDeg(450 - ang), y = aH * sinDeg(ang);
        var px = cX + x, py = cY + y;
        var ptx = cvRound(px / XY_ONE) * XY_ONE, pty = cvRound(py / XY_ONE) * XY_ONE;
        ptx += cvRound(px - ptx);
        pty += cvRound(py - pty);
        if (!prev || prev[0] !== ptx || prev[1] !== pty) {
          v.push([ptx, pty]);
          prev = [ptx, pty];
        }
      }
      if (v.length === 1) v = [[cX, cY], [cX, cY]];
      return v;
    }
    function fillConvexPoly(mask, w, h, v) {
      var npts = v.length, delta = XY_ONE >> 1, delta1 = XY_ONE >> 1, delta2 = XY_ONE >> 1;
      var imin = 0, xmin = v[0][0], xmax = v[0][0], ymin = v[0][1], ymax = v[0][1];
      var p0 = v[npts - 1];
      for (var i = 0; i < npts; i++) {
        var p = v[i];
        if (p[1] < ymin) {
          ymin = p[1];
          imin = i;
        }
        ymax = Math.max(ymax, p[1]);
        xmax = Math.max(xmax, p[0]);
        xmin = Math.min(xmin, p[0]);
        line2(mask, w, h, p0[0], p0[1], p[0], p[1]);
        p0 = p;
      }
      xmin = floorDiv(xmin + delta, XY_SHIFT);
      xmax = floorDiv(xmax + delta, XY_SHIFT);
      ymin = floorDiv(ymin + delta, XY_SHIFT);
      ymax = floorDiv(ymax + delta, XY_SHIFT);
      if (npts < 3 || xmax < 0 || ymax < 0 || xmin >= w || ymin >= h) return;
      ymax = Math.min(ymax, h - 1);
      var edge = [
        { idx: imin, di: 1, x: -XY_ONE, dx: 0, ye: ymin },
        { idx: imin, di: npts - 1, x: -XY_ONE, dx: 0, ye: ymin },
      ];
      var edges = npts, y = ymin;
      do {
        for (var e = 0; e < 2; e++) {
          if (y >= edge[e].ye) {
            var idx0 = edge[e].idx, di = edge[e].di, idx = idx0 + di;
            if (idx >= npts) idx -= npts;
            var ty = 0;
            for (; edges-- > 0; ) {
              ty = floorDiv(v[idx][1] + delta, XY_SHIFT);
              if (ty > y) {
                var xs = v[idx0][0], xe = v[idx][0];
                edge[e].ye = ty;
                edge[e].dx = Math.trunc(((xe - xs) * 2 + (ty - y)) / (2 * (ty - y)));
                edge[e].x = xs;
                edge[e].idx = idx;
                break;
              }
              idx0 = idx;
              idx += di;
              if (idx >= npts) idx -= npts;
            }
          }
        }
        if (edges < 0) break;
        if (y >= 0) {
          var left = 0, right = 1;
          if (edge[0].x > edge[1].x) {
            left = 1;
            right = 0;
          }
          var xx1 = floorDiv(edge[left].x + delta1, XY_SHIFT), xx2 = floorDiv(edge[right].x + delta2, XY_SHIFT);
          if (xx2 >= 0 && xx1 < w) {
            if (xx1 < 0) xx1 = 0;
            if (xx2 >= w) xx2 = w - 1;
            for (var xx = xx1; xx <= xx2; xx++) mask[y * w + xx] = 1;
          }
        }
        edge[0].x += edge[0].dx;
        edge[1].x += edge[1].dx;
      } while (++y <= ymax);
    }

    // ------------------------------------------------------------------------
    // Template parsing (src/template.py, src/utils/parsing.py)
    // ------------------------------------------------------------------------
    var FIELD_STRING_REGEX = /([^\.\d]+)(\d+)\.{2,3}(\d+)/;
    function parseFieldString(fieldString) {
      if (fieldString.indexOf(".") >= 0) {
        var m = FIELD_STRING_REGEX.exec(fieldString);
        if (!m) throw new Error("Invalid field string: '" + fieldString + "'");
        var start = parseInt(m[2], 10), end = parseInt(m[3], 10);
        if (start >= end) throw new Error("Invalid range in fields string: '" + fieldString + "', start: " + start + " is not less than end: " + end);
        var out = [];
        for (var n = start; n <= end; n++) out.push(m[1] + n);
        return out;
      }
      return [fieldString];
    }
    function parseFields(key, fields) {
      var parsed = [], seen = {};
      (fields || []).forEach(function (fs) {
        var arr = parseFieldString(fs);
        arr.forEach(function (f) {
          if (seen[f]) throw new Error("Given field string '" + fs + "' has overlapping field(s) with other fields in '" + key + "'");
        });
        arr.forEach(function (f) {
          seen[f] = true;
          parsed.push(f);
        });
      });
      return parsed;
    }
    function naturalKey(label) {
      var m = /([^\d]+)(\d*)/.exec(label);
      return m ? [m[1], m[2] ? parseInt(m[2], 10) : 0] : [label, 0];
    }
    function naturalCompare(a, b) {
      var ka = naturalKey(a), kb = naturalKey(b);
      if (ka[0] < kb[0]) return -1;
      if (ka[0] > kb[0]) return 1;
      return ka[1] - kb[1];
    }

    function buildFieldBlock(name, obj, defaults) {
      if (obj.fieldType) {
        var ft = FIELD_TYPES[obj.fieldType];
        if (!ft) throw new Error("Unknown fieldType '" + obj.fieldType + "' in block " + name);
        obj = Object.assign({}, ft, obj);
      } else obj = Object.assign({}, obj, { fieldType: "__CUSTOM__" });
      obj = Object.assign({ direction: "vertical", emptyValue: defaults.emptyValue, bubbleDimensions: defaults.bubbleDimensions }, obj);
      var labels = parseFields("Field Block Labels: " + name, obj.fieldLabels);
      var values = obj.bubbleValues, gap = obj.bubblesGap, lgap = obj.labelsGap, dims = obj.bubbleDimensions;
      var vertical = obj.direction === "vertical";
      var _h = vertical ? 1 : 0, _v = vertical ? 0 : 1;
      var valuesDim = Math.trunc(gap * (values.length - 1) + dims[_h]);
      var fieldsDim = Math.trunc(lgap * (labels.length - 1) + dims[_v]);
      var dimensions = vertical ? [fieldsDim, valuesDim] : [valuesDim, fieldsDim];
      var fields = [];
      var lead = [Number(obj.origin[0]), Number(obj.origin[1])];
      labels.forEach(function (label) {
        var pt = lead.slice(), bubbles = [];
        values.forEach(function (value) {
          bubbles.push({ x: roundHalfEven(pt[0]), y: roundHalfEven(pt[1]), label: label, value: String(value), dx: 0, dy: 0 });
          pt[_h] += gap;
        });
        fields.push({ label: label, bubbles: bubbles });
        lead[_v] += lgap;
      });
      return {
        name: name,
        origin: obj.origin.slice(),
        dimensions: dimensions,
        bubbleDimensions: dims.slice(),
        emptyValue: obj.emptyValue,
        fieldType: obj.fieldType,
        direction: obj.direction,
        labels: labels,
        fields: fields,
        shift: 0,
        shiftY: 0,
        // border rectification: null inherits alignment_params.rectify_on_border
        rectifyOnBorder: obj.rectifyOnBorder === undefined ? null : obj.rectifyOnBorder,
        borderPadding: obj.borderPadding === undefined ? null : obj.borderPadding,
        rectified: false,
      };
    }

    function parseTemplate(json) {
      var t = Object.assign({ preProcessors: [], emptyValue: "", customLabels: {}, outputColumns: [], zones: {} }, json);
      ["bubbleDimensions", "pageDimensions", "fieldBlocks"].forEach(function (k) {
        if (!t[k]) throw new Error("Template is missing '" + k + "'");
      });
      var page = t.pageDimensions, all = {}, blocks = [];
      Object.keys(t.fieldBlocks).forEach(function (name) {
        var block = buildFieldBlock(name, Object.assign({}, t.fieldBlocks[name]), { emptyValue: t.emptyValue, bubbleDimensions: t.bubbleDimensions });
        block.labels.forEach(function (l) {
          if (all[l]) throw new Error("The field strings for field block " + name + " overlap with other existing fields");
        });
        block.labels.forEach(function (l) {
          all[l] = true;
        });
        var ex = block.origin[0] + block.dimensions[0], ey = block.origin[1] + block.dimensions[1];
        if (ex >= page[0] || ey >= page[1] || block.origin[0] < 0 || block.origin[1] < 0)
          throw new Error("Overflowing field block '" + name + "' with origin " + JSON.stringify(block.origin) + " and dimensions " + JSON.stringify(block.dimensions) + " in template with dimensions " + JSON.stringify(page));
        blocks.push(block);
      });
      var zones = [];
      Object.keys(t.zones || {}).forEach(function (name) {
        var z = t.zones[name];
        if (all[name]) throw new Error("Zone name '" + name + "' overlaps with an existing field label");
        var x = z.origin[0], y = z.origin[1], w = z.dimensions[0], h = z.dimensions[1];
        if (x + w > page[0] || y + h > page[1]) throw new Error("Overflowing zone '" + name + "'");
        var options = z.options || {};
        zones.push({
          name: name,
          type: z.type,
          origin: [x, y],
          dimensions: [w, h],
          options: options,
          emptyValue: options.emptyValue !== undefined ? options.emptyValue : "",
          // lazy zones are read only when a check needs them as a fallback
          lazy: !!options.lazy,
          // null: read the page image; otherwise this zone's own colour dropout variant
          colorDropout: "colorDropout" in options ? normalizeDropout(options.colorDropout) : null,
        });
        all[name] = true;
      });
      var customLabels = {}, customFields = {};
      Object.keys(t.customLabels || {}).forEach(function (label) {
        var parsed = parseFields("Custom Label: " + label, t.customLabels[label]);
        var missing = parsed.filter(function (p) {
          return !all[p];
        });
        if (missing.length) throw new Error("Missing field block label(s) in the given template for " + JSON.stringify(missing) + " from '" + label + "'");
        parsed.forEach(function (p) {
          if (customFields[p]) throw new Error("The field strings for custom label '" + label + "' overlap with other existing custom labels");
        });
        parsed.forEach(function (p) {
          customFields[p] = true;
        });
        customLabels[label] = parsed;
      });
      var nonCustom = Object.keys(all).filter(function (l) {
        return !customFields[l];
      });
      var parsed = {
        json: json,
        pageDimensions: page.slice(),
        bubbleDimensions: t.bubbleDimensions.slice(),
        emptyValue: t.emptyValue,
        preProcessors: t.preProcessors || [],
        fieldBlocks: blocks,
        zones: zones,
        customLabels: customLabels,
        nonCustomLabels: nonCustom,
        allLabels: Object.keys(all),
        colorDropout: normalizeDropout(t.colorDropout),
      };
      // optional "validate" and "checks"; check outputs become columns
      // Optional per-group placeholders (see joinGroup); unknown groups are ignored
      parsed.groupOptions = {};
      Object.keys(t.groupOptions || {}).forEach(function (name) {
        if (has(customLabels, name)) parsed.groupOptions[name] = Object.assign({}, t.groupOptions[name] || {});
      });
      parsed.rules = new RuleSet(parsed, t.validate, t.checks);
      var outputColumns = parseFields("Output Columns", t.outputColumns || []);
      if (!outputColumns.length) outputColumns = nonCustom.concat(Object.keys(customLabels), parsed.rules.newOutputColumns).sort(naturalCompare);
      parsed.outputColumns = outputColumns;
      return parsed;
    }

    // ------------------------------------------------------------------------
    // Preprocessors
    // ------------------------------------------------------------------------
    function TimingMarkAlignment(options, page) {
      var self = this;
      this.name = "TimingMarkAlignment";
      this.needsFullResolution = true;
      this.geometry = "recorded";
      this.page = page;
      var tracks = options.tracks || {};
      this.trackNames = Object.keys(tracks);
      this.expected = [];
      var spacings = [];
      this.tracks = [];
      this.trackNames.forEach(function (n) {
        var marks = tracks[n].marks;
        self.tracks.push(marks.map(function (m) {
          return [Number(m[0]), Number(m[1])];
        }));
        marks.forEach(function (m) {
          self.expected.push([Number(m[0]), Number(m[1])]);
        });
        if (marks.length > 1) {
          var mn = Infinity;
          for (var i = 1; i < marks.length; i++) mn = Math.min(mn, Math.hypot(marks[i][0] - marks[i - 1][0], marks[i][1] - marks[i - 1][1]));
          spacings.push(mn);
        }
      });
      // np.float32 template points
      this.expected = this.expected.map(function (p) {
        return [Math.fround(p[0]), Math.fround(p[1])];
      });
      this.markW = options.markDimensions[0];
      this.markH = options.markDimensions[1];
      this.sizeTolerance = options.sizeTolerance !== undefined ? options.sizeTolerance : TM_DEFAULT_SIZE_TOLERANCE;
      var minSpacing = spacings.length ? Math.min.apply(null, spacings) : 50.0;
      this.minSpacing = minSpacing;
      this.searchRadius = options.searchRadius !== undefined ? options.searchRadius : 0.45 * minSpacing;
      this.minMatched = options.minMatchedMarks !== undefined ? options.minMatchedMarks : Math.min(TM_DEFAULT_MIN_MATCHED, this.expected.length);
      this.maxResidual = options.maxResidual !== undefined ? options.maxResidual : TM_DEFAULT_MAX_RESIDUAL;
      this.nonRigid = !!options.nonRigid;
      this.detectOrientation = options.detectOrientation !== undefined ? !!options.detectOrientation : true;
    }
    TimingMarkAlignment.prototype.apply = function (image, ctx) {
      var pageW = this.page[0], pageH = this.page[1];
      var corners = findPageCorners(image);
      var candidates = this.blobCentres(image, corners);
      ctx.registration = { method: "timing_marks", candidates: candidates.length };
      if (candidates.length < this.minMatched) {
        ctx.registration.error = "Timing marks not found: " + candidates.length + " candidate blobs";
        return null;
      }
      var rotations = this.detectOrientation ? [0, 1, 2, 3] : [0];
      var best = null;
      for (var r = 0; r < rotations.length; r++) {
        var fit = this.fitOrientation(corners, candidates, rotations[r]);
        if (!fit) continue;
        if (!best || fit.matched > best.matched || (fit.matched === best.matched && fit.residual < best.residual)) best = fit;
      }
      if (!best || best.matched < this.minMatched) {
        ctx.registration.error = "Timing mark registration failed: matched " + (best ? best.matched : 0) + "/" + this.expected.length + " marks";
        return null;
      }
      if (best.residual > this.maxResidual) {
        ctx.registration.error = "Timing mark registration rejected: residual " + best.residual.toFixed(2) + "px > " + this.maxResidual + "px";
        return null;
      }
      var H = best.homography, pw = Math.trunc(pageW), ph = Math.trunc(pageH);
      function warpPage(im) {
        return warpPerspective(im, H, pw, ph, 255);
      }
      var warped = warpPage(image);
      recordGeometry(ctx, warpPage);
      if (this.nonRigid && best.matched >= 6) warped = thinPlateCorrection(warped, best, ctx);
      ctx.registration = {
        method: "timing_marks",
        orientation: best.rotation * 90,
        matched_marks: best.matched,
        expected_marks: this.expected.length,
        residual_px: roundTo(best.residual, 3),
        homography: best.homography,
      };
      return warped;
    };
    TimingMarkAlignment.prototype.coarseHomography = function (corners, rotation) {
      var w = this.page[0], h = this.page[1];
      var rotated = [0, 1, 2, 3].map(function (i) {
        return corners[(i + rotation) % 4];
      });
      return getPerspectiveTransform([[0, 0], [w, 0], [w, h], [0, h]], rotated);
    };
    TimingMarkAlignment.prototype.blobCentres = function (image, corners) {
      var pageW = this.page[0], pageH = this.page[1];
      var sideA = Math.hypot(corners[1][0] - corners[0][0], corners[1][1] - corners[0][1]);
      var sideB = Math.hypot(corners[3][0] - corners[0][0], corners[3][1] - corners[0][1]);
      var scale = Math.sqrt((sideA * sideB) / (pageW * pageH));
      var expectedArea = this.markW * this.markH * scale * scale;
      var low = expectedArea * Math.pow(1 - this.sizeTolerance, 2), high = expectedArea * Math.pow(1 + this.sizeTolerance, 2);
      var shrink = Math.min(1.0, 8.0 / Math.max(Math.min(this.markW, this.markH) * scale, 1e-6));
      var img = image;
      if (shrink < 0.9) {
        img = resizeArea(image, cvRound(image.width * shrink), cvRound(image.height * shrink), shrink, shrink);
      } else shrink = 1.0;
      scale *= shrink;
      low *= shrink * shrink;
      high *= shrink * shrink;
      var block = Math.trunc(Math.max(15, Math.floor((Math.max(this.markW, this.markH) * scale * 4) / 2) * 2 + 1));
      var bin = adaptiveMeanThresholdInv(img, block, 15);
      var comps = connectedComponents(bin).components;
      var longSide = Math.max(this.markW, this.markH) * scale;
      var out = [];
      for (var i = 0; i < comps.length; i++) {
        var c = comps[i];
        var solidity = c.area / Math.max(c.w * c.h, 1);
        if (c.area >= low && c.area <= high && solidity > 0.6 && Math.max(c.w, c.h) <= longSide * (1 + this.sizeTolerance) * 1.5) {
          out.push([Math.fround(c.cx / shrink), Math.fround(c.cy / shrink)]);
        }
      }
      return out;
    };
    TimingMarkAlignment.prototype.pixelsPerUnit = function (H) {
      var cx = this.page[0] / 2, cy = this.page[1] / 2;
      var a = projectPoint(H, cx, cy), b = projectPoint(H, cx + 1, cy);
      return Math.hypot(b[0] - a[0], b[1] - a[1]);
    };
    TimingMarkAlignment.prototype.match = function (H, candidates, radius) {
      var proj = projectPoints(H, this.expected), ne = proj.length, nc = candidates.length;
      var dist = new Float64Array(ne * nc);
      var nearestC = new Int32Array(ne), nearestE = new Int32Array(nc);
      var bestE = new Float64Array(nc).fill(Infinity);
      for (var i = 0; i < ne; i++) {
        var bd = Infinity, bj = 0;
        for (var j = 0; j < nc; j++) {
          var d = Math.hypot(proj[i][0] - candidates[j][0], proj[i][1] - candidates[j][1]);
          dist[i * nc + j] = d;
          if (d < bd) {
            bd = d;
            bj = j;
          }
          if (d < bestE[j]) {
            bestE[j] = d;
            nearestE[j] = i;
          }
        }
        nearestC[i] = bj;
      }
      var pairs = [];
      for (var k = 0; k < ne; k++) {
        var jj = nearestC[k];
        if (nearestE[jj] === k && dist[k * nc + jj] <= radius) pairs.push([k, jj]);
      }
      return pairs;
    };
    function spansTwoDimensions(points) {
      var n = points.length;
      if (n < 3) return false;
      var mx = 0, my = 0;
      points.forEach(function (p) {
        mx += p[0];
        my += p[1];
      });
      mx /= n;
      my /= n;
      var sxx = 0, syy = 0, sxy = 0;
      points.forEach(function (p) {
        var dx = p[0] - mx, dy = p[1] - my;
        sxx += dx * dx;
        syy += dy * dy;
        sxy += dx * dy;
      });
      sxx /= n - 1;
      syy /= n - 1;
      sxy /= n - 1;
      var tr = sxx + syy, det = sxx * syy - sxy * sxy, disc = Math.sqrt(Math.max(tr * tr / 4 - det, 0));
      var e0 = tr / 2 - disc, e1 = tr / 2 + disc;
      return e0 > 1e-3 * e1 && e0 > 25;
    }
    // Best fit for one orientation over a few shifted starting guesses: tracks are
    // periodic, so a coarse guess about one pitch off (a page scanned flush and
    // tilted) can lock a track onto its neighbouring mark (fit_orientation)
    TimingMarkAlignment.prototype.fitOrientation = function (corners, candidates, rotation) {
      var coarse = this.coarseHomography(corners, rotation);
      if (!coarse) return null;
      var radius = this.searchRadius * this.pixelsPerUnit(coarse);
      var guesses = this.startingGuesses(coarse, candidates);
      var best = null, seen = [];
      for (var g = 0; g < guesses.length; g++) {
        var fit = this.refine(guesses[g], candidates, radius, 1234 + rotation * 7 + g * 13);
        if (!fit) continue;
        var duplicate = seen.some(function (h) {
          for (var k = 0; k < 9; k++) if (Math.abs(h[k] - fit.homography[k]) > 1e-3) return false;
          return true;
        });
        if (duplicate) continue;
        seen.push(fit.homography);
        fit.rotation = rotation;
        fit.beyondEnds = this.marksBeyondTrackEnds(fit.homography, candidates, radius * 0.5);
        if (!best || fitKeyGreater(fit, best)) best = fit;
        if (fit.beyondEnds === 0 && fit.matched >= TM_GOOD_FIT_FRACTION * this.expected.length) break;
      }
      return best;
    };
    function fitKeyGreater(a, b) {
      var ka = a.matched - 2 * a.beyondEnds, kb = b.matched - 2 * b.beyondEnds;
      return ka > kb || (ka === kb && a.residual < b.residual);
    }
    // The coarse mapping turned by the tracks' measured tilt, then shifted by half
    // and one mark pitch (_starting_guesses)
    TimingMarkAlignment.prototype.startingGuesses = function (coarse, candidates) {
      var cx = this.page[0] / 2, cy = this.page[1] / 2, pitch = this.minSpacing;
      var tilt = this.estimateTilt(coarse, candidates, pitch);
      var angles = Math.abs(tilt) > TM_MIN_TILT_DEGREES ? [tilt, 0.0] : [0.0];
      var guesses = [];
      angles.forEach(function (angle) {
        // cv2.getRotationMatrix2D(centre, angle, 1)
        var t = (angle * Math.PI) / 180, al = Math.cos(t), be = Math.sin(t);
        var rotate = [al, be, (1 - al) * cx - be * cy, -be, al, be * cx + (1 - al) * cy, 0, 0, 1];
        [0.0, -0.5, 0.5, -1.0, 1.0].forEach(function (dy) {
          [0.0, -0.5, 0.5].forEach(function (dx) {
            var shift = [1, 0, dx * pitch, 0, 1, dy * pitch, 0, 0, 1];
            guesses.push(mul3x3(mul3x3(coarse, shift), rotate));
          });
        });
      });
      return guesses;
    };
    // Degrees the marks are turned from the template, from neighbour directions (_estimate_tilt)
    TimingMarkAlignment.prototype.estimateTilt = function (coarse, candidates, pitch) {
      if (candidates.length < 6) return 0.0;
      var inv = invert3x3(coarse);
      if (!inv) return 0.0;
      var pts = projectPoints(inv, candidates), steps = [];
      for (var i = 0; i < pts.length; i++) {
        var bd = Infinity, bj = -1;
        for (var j = 0; j < pts.length; j++) {
          if (j === i) continue;
          var dx = pts[j][0] - pts[i][0], dy = pts[j][1] - pts[i][1], d = dx * dx + dy * dy;
          if (d < bd) {
            bd = d;
            bj = j;
          }
        }
        var step = [pts[bj][0] - pts[i][0], pts[bj][1] - pts[i][1]];
        if (Math.abs(Math.hypot(step[0], step[1]) - pitch) < 0.25 * pitch) steps.push(step);
      }
      if (steps.length < 6) return 0.0;
      var templateSteps = [];
      for (var k = 1; k < this.expected.length; k++) {
        var s = [this.expected[k][0] - this.expected[k - 1][0], this.expected[k][1] - this.expected[k - 1][1]];
        if (Math.abs(Math.hypot(s[0], s[1]) - pitch) < 0.25 * pitch) templateSteps.push(s);
      }
      var expected = templateSteps.length ? angleMod90(templateSteps) : 0.0;
      // Template -> image rotation; getRotationMatrix2D turns the other way
      return -(pyMod(angleMod90(steps) - expected + 45, 90) - 45);
    };
    function pyMod(a, n) {
      return ((a % n) + n) % n;
    }
    // Median direction of step vectors, folded into [-45, 45) degrees
    function angleMod90(steps) {
      var angles = steps
        .map(function (s) {
          return pyMod((Math.atan2(s[1], s[0]) * 180) / Math.PI + 45, 90) - 45;
        })
        .sort(function (a, b) {
          return a - b;
        });
      var m = angles.length >> 1;
      return angles.length % 2 ? angles[m] : (angles[m - 1] + angles[m]) / 2;
    }
    // Blobs one pitch past either end of a track: the fit slid along it
    TimingMarkAlignment.prototype.marksBeyondTrackEnds = function (H, candidates, radius) {
      var beyond = [];
      this.tracks.forEach(function (m) {
        if (m.length < 2) return;
        var n = m.length;
        beyond.push([2 * m[0][0] - m[1][0], 2 * m[0][1] - m[1][1]]);
        beyond.push([2 * m[n - 1][0] - m[n - 2][0], 2 * m[n - 1][1] - m[n - 2][1]]);
      });
      if (!beyond.length || !candidates.length) return 0;
      var count = 0;
      projectPoints(H, beyond).forEach(function (p) {
        for (var j = 0; j < candidates.length; j++) {
          if (Math.hypot(p[0] - candidates[j][0], p[1] - candidates[j][1]) <= radius) {
            count++;
            break;
          }
        }
      });
      return count;
    };
    TimingMarkAlignment.prototype.refine = function (H, candidates, radius, seed) {
      var self = this;
      var attempts = [radius * 2.0, radius, radius * 0.5];
      function pts(pairs) {
        return {
          t: pairs.map(function (p) {
            return self.expected[p[0]];
          }),
          i: pairs.map(function (p) {
            return candidates[p[1]];
          }),
        };
      }
      for (var a = 0; a < attempts.length; a++) {
        var pairs = this.match(H, candidates, attempts[a]);
        if (pairs.length < Math.max(4, Math.floor(this.minMatched / 2))) return null;
        var p = pts(pairs);
        if (!spansTwoDimensions(p.t)) return null;
        var fit = findHomographyRansac(p.t, p.i, Math.max(2.0, 0.5 * radius), seed + a);
        if (!fit) return null;
        H = fit.H;
      }
      var finalPairs = this.match(H, candidates, radius * 0.5);
      if (finalPairs.length < 4) return null;
      var fp = pts(finalPairs);
      var Hf = findHomographyLS(fp.t, fp.i);
      if (!Hf) return null;
      var inv = invert3x3(Hf);
      if (!inv) return null;
      var res = 0;
      for (var k = 0; k < fp.i.length; k++) {
        var b = projectPoint(inv, fp.i[k][0], fp.i[k][1]);
        res += Math.hypot(b[0] - fp.t[k][0], b[1] - fp.t[k][1]);
      }
      res /= fp.i.length;
      return { homography: Hf, matched: finalPairs.length, residual: res, templatePts: fp.t, imagePts: fp.i };
    };

    // Register a pure geometric image -> image step to replay on companion images
    // (colour dropout variants some zones read); see ImagePreprocessor.record_geometry
    function recordGeometry(ctx, fn) {
      if (ctx && ctx.geometry) ctx.geometry.push(fn);
    }

    // Coarse page outline (TimingMarkAlignment.find_page_corners)
    function findPageCorners(image) {
      var h = image.height, w = image.width;
      var scale = 800.0 / Math.max(h, w), small = image;
      if (scale < 1) small = resizeLinear(image, cvRound(w * scale), cvRound(h * scale));
      scale = Math.min(scale, 1.0);
      var blurred = gaussianBlur(small, 5, 5, 0);
      var bin = thresholdBinary(blurred, otsuValue(blurred), false);
      bin = morphClose(bin, 9, 9);
      var full = [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]];
      var region = largestRegionQuad(bin, 0.02);
      if (!region || region.area < TM_MIN_PAGE_AREA_FRACTION * small.width * small.height) return full;
      var quad = region.quad.map(function (p) {
        return [p[0] / scale, p[1] / scale];
      });
      return orderPoints(quad);
    }

    // Thin-plate-spline residual correction (TimingMarkAlignment.thin_plate_correction)
    function tpsKernel(r) {
      return r > 0 ? r * r * Math.log(r) : 0;
    }
    function thinPlateCorrection(warped, fit, ctx) {
      var inv = invert3x3(fit.homography);
      var observed = projectPoints(inv, fit.imagePts), targets = fit.templatePts, n = targets.length;
      var A = [], bx = [], by = [];
      for (var i = 0; i < n + 3; i++) A.push(new Array(n + 3).fill(0));
      for (var i2 = 0; i2 < n; i2++) {
        for (var j = 0; j < n; j++) A[i2][j] = tpsKernel(Math.hypot(targets[i2][0] - targets[j][0], targets[i2][1] - targets[j][1])) + (i2 === j ? 1.0 : 0);
        A[i2][n] = 1;
        A[i2][n + 1] = targets[i2][0];
        A[i2][n + 2] = targets[i2][1];
        A[n][i2] = 1;
        A[n + 1][i2] = targets[i2][0];
        A[n + 2][i2] = targets[i2][1];
        bx.push(observed[i2][0] - targets[i2][0]);
        by.push(observed[i2][1] - targets[i2][1]);
      }
      bx.push(0, 0, 0);
      by.push(0, 0, 0);
      var wx = solveLinear(A, bx), wy = solveLinear(A, by);
      if (!wx || !wy) return warped;
      var pw = warped.width, ph = warped.height;
      var gw = Math.ceil((pw + TPS_GRID_STEP) / TPS_GRID_STEP), gh = Math.ceil((ph + TPS_GRID_STEP) / TPS_GRID_STEP);
      var gx = new Float64Array(gw * gh), gy = new Float64Array(gw * gh);
      for (var r = 0; r < gh; r++)
        for (var c = 0; c < gw; c++) {
          var x = c * TPS_GRID_STEP, y = r * TPS_GRID_STEP, dx = wx[n] + wx[n + 1] * x + wx[n + 2] * y, dy = wy[n] + wy[n + 1] * x + wy[n + 2] * y;
          for (var k = 0; k < n; k++) {
            var kv = tpsKernel(Math.hypot(x - targets[k][0], y - targets[k][1]));
            dx += kv * wx[k];
            dy += kv * wy[k];
          }
          gx[r * gw + c] = dx;
          gy[r * gw + c] = dy;
        }
      // cv2.resize(INTER_LINEAR) of the coarse displacement grid to page size
      var xt = linearTab(gw, pw, gw / pw, false), yt = linearTab(gh, ph, gh / ph, false);
      var mapX = new Float32Array(pw * ph), mapY = new Float32Array(pw * ph);
      for (var yy = 0; yy < ph; yy++) {
        var y0 = yt.ofs[yy], y1 = Math.min(y0 + 1, gh - 1), fy = yt.alpha[yy];
        for (var xx = 0; xx < pw; xx++) {
          var x0 = xt.ofs[xx], x1 = Math.min(x0 + 1, gw - 1), fx = xt.alpha[xx];
          var ddx = (gx[y0 * gw + x0] * (1 - fx) + gx[y0 * gw + x1] * fx) * (1 - fy) + (gx[y1 * gw + x0] * (1 - fx) + gx[y1 * gw + x1] * fx) * fy;
          var ddy = (gy[y0 * gw + x0] * (1 - fx) + gy[y0 * gw + x1] * fx) * (1 - fy) + (gy[y1 * gw + x0] * (1 - fx) + gy[y1 * gw + x1] * fx) * fy;
          mapX[yy * pw + xx] = xx + ddx;
          mapY[yy * pw + xx] = yy + ddy;
        }
      }
      function remap(im) {
        return remapLinear(im, mapX, mapY, pw, ph, 255);
      }
      recordGeometry(ctx, remap);
      return remap(warped);
    }

    // CropPage: page outline -> warp. Python finds the page with Canny edges on the
    // processing-size image; here the bright page region is segmented instead.
    function CropPage(options) {
      this.name = "CropPage";
      this.needsFullResolution = false;
      this.geometry = "recorded";
      this.morphKernel = (options && options.morphKernel) || [10, 10];
    }
    CropPage.prototype.apply = function (image, ctx) {
      var blurred = normalizeMinMax(gaussianBlur(image, 3, 3, 0));
      // Fixed thresholds miss white-on-white or dark sheets; retry adaptively
      var quad = findPageQuad(blurred, this.morphKernel) || findPageQuadAdaptive(blurred, this.morphKernel);
      if (!quad) {
        ctx.registration = { method: "crop_page", error: "Paper boundary not found" };
        return null;
      }
      ctx.registration = { method: "crop_page", corners: quad };
      recordGeometry(ctx, function (im) {
        return fourPointTransform(im, quad);
      });
      return fourPointTransform(blurred, quad);
    };
    // cv2.Canny(img, low, high) with a 3x3 Sobel and the L1 gradient norm
    function canny(img, low, high) {
      if (low > high) {
        var t = low;
        low = high;
        high = t;
      }
      var w = img.width, h = img.height, d = img.data, n = w * h;
      var dx = new Int32Array(n), dy = new Int32Array(n), mag = new Int32Array(n);
      for (var y = 0; y < h; y++) {
        var ym = (y > 0 ? y - 1 : 0) * w, y0 = y * w, yp = (y < h - 1 ? y + 1 : h - 1) * w;
        for (var x = 0; x < w; x++) {
          var xm = x > 0 ? x - 1 : 0, xp = x < w - 1 ? x + 1 : w - 1;
          var gx = d[ym + xp] - d[ym + xm] + 2 * (d[y0 + xp] - d[y0 + xm]) + d[yp + xp] - d[yp + xm];
          var gy = d[yp + xm] - d[ym + xm] + 2 * (d[yp + x] - d[ym + x]) + d[yp + xp] - d[ym + xp];
          dx[y0 + x] = gx;
          dy[y0 + x] = gy;
          mag[y0 + x] = (gx < 0 ? -gx : gx) + (gy < 0 ? -gy : gy);
        }
      }
      // 0 = no edge, 1 = weak, 2 = strong
      var map = new Uint8Array(n), stack = [], TG22 = 13573;
      for (var y2 = 1; y2 < h - 1; y2++) {
        for (var x2 = 1; x2 < w - 1; x2++) {
          var i = y2 * w + x2, m = mag[i];
          if (m <= low) continue;
          var xs = dx[i], ys = dy[i], ax = xs < 0 ? -xs : xs, ay = (ys < 0 ? -ys : ys) * 32768, tg22x = ax * TG22, keep;
          if (ay < tg22x) keep = m > mag[i - 1] && m >= mag[i + 1];
          else {
            var tg67x = tg22x + ax * 65536;
            if (ay > tg67x) keep = m > mag[i - w] && m >= mag[i + w];
            else {
              var sgn = (xs ^ ys) < 0 ? -1 : 1;
              keep = m > mag[i - w - sgn] && m > mag[i + w + sgn];
            }
          }
          if (!keep) continue;
          if (m > high) {
            map[i] = 2;
            stack.push(i);
          } else map[i] = 1;
        }
      }
      while (stack.length) {
        var c = stack.pop();
        for (var k = 0; k < 8; k++) {
          var q = c + (k < 3 ? -w - 1 + k : k === 3 ? -1 : k === 4 ? 1 : w - 6 + k - 1 + 1);
          if (q >= 0 && q < n && map[q] === 1) {
            map[q] = 2;
            stack.push(q);
          }
        }
      }
      var out = new Uint8Array(n);
      for (var j = 0; j < n; j++) out[j] = map[j] === 2 ? 255 : 0;
      return makeImage(w, h, out);
    }
    // CropPage.find_page: truncate, close, Canny, then the largest rectangular hull
    // CropPage.min_page_area: the page covers a fixed share of the frame at any size
    var DEFAULT_PROCESSING_AREA = 666 * 820;
    function minPageArea(image) {
      return (MIN_PAGE_AREA_THRESHOLD * (image.width * image.height)) / DEFAULT_PROCESSING_AREA;
    }
    function findPageQuad(image, morphKernel) {
      var norm = normalizeMinMax(image);
      var lut = new Uint8Array(256);
      for (var v = 0; v < 256; v++) lut[v] = v > 200 ? 200 : v;
      var trunc = normalizeMinMax(applyLut(norm, lut));
      var closed = morphRect(morphRect(trunc, morphKernel[0], morphKernel[1], true), morphKernel[0], morphKernel[1], false);
      var edge = canny(closed, 185, 55);
      return pageQuadFromEdges([edge], minPageArea(image), 5);
    }
    // CropPage.find_page_adaptive: Otsu + auto-Canny edges of the binary and blurred page
    function findPageQuadAdaptive(image, morphKernel) {
      var blurred = gaussianBlur(image, 5, 5, 0), t = otsuValue(blurred);
      var binary = thresholdBinary(blurred, t, false);
      var closed = morphRect(morphRect(binary, morphKernel[0], morphKernel[1], true), morphKernel[0], morphKernel[1], false);
      return pageQuadFromEdges([autoCanny(closed), autoCanny(blurred)], minPageArea(image), 10);
    }
    // ImageUtils.auto_canny (sigma 0.93 around the median)
    function autoCanny(img) {
      var hist = new Uint32Array(256), d = img.data, n = d.length, i;
      for (i = 0; i < n; i++) hist[d[i]]++;
      function nth(k) {
        for (var v = 0, c = 0; v < 256; v++) {
          c += hist[v];
          if (c > k) return v;
        }
        return 255;
      }
      var median = n % 2 ? nth((n - 1) / 2) : (nth(n / 2 - 1) + nth(n / 2)) / 2;
      var lower = Math.trunc(Math.max(0, (1.0 - 0.93) * median)), upper = Math.trunc(Math.min(255, (1.0 + 0.93) * median));
      return canny(img, lower, upper);
    }
    // Convex hulls of the edge contours, largest first; the first 4-corner,
    // near-rectangular approximation among the `limit` largest is the page
    function pageQuadFromEdges(edges, minArea, limit) {
      var hulls = [];
      edges.forEach(function (edge) {
        var cc = connectedComponents(edge), runs = cc.runs, comps = cc.components;
        // per component: extreme points of each row -> convex hull
        var rowsByComp = {};
        for (var r = 0; r < runs.count; r++) {
          var lab = runs.label[r], yy = runs.y[r];
          var e = rowsByComp[lab] || (rowsByComp[lab] = {});
          var cur = e[yy];
          if (!cur) e[yy] = [runs.x0[r], runs.x1[r]];
          else {
            if (runs.x0[r] < cur[0]) cur[0] = runs.x0[r];
            if (runs.x1[r] > cur[1]) cur[1] = runs.x1[r];
          }
        }
        comps.forEach(function (comp) {
          // a hull is never larger than its bounding box: skip what can't qualify
          if (comp.w * comp.h < minArea) return;
          var rows = rowsByComp[comp.label], pts = [];
          Object.keys(rows).forEach(function (k) {
            var yk = Number(k);
            pts.push([rows[k][0], yk]);
            if (rows[k][1] !== rows[k][0]) pts.push([rows[k][1], yk]);
          });
          var hull = convexHull(pts);
          if (hull.length >= 3) hulls.push({ hull: hull, area: polygonArea(hull) });
        });
      });
      hulls.sort(function (a, b) {
        return b.area - a.area;
      });
      for (var hi = 0; hi < hulls.length && hi < limit; hi++) {
        if (hulls[hi].area < minArea) continue;
        var hull2 = hulls[hi].hull;
        var approx = approxPolyClosed(hull2, APPROX_POLY_EPSILON_FACTOR * perimeter(hull2));
        if (approx.length !== 4) continue;
        var ok = true;
        for (var i = 2; i < 5; i++) {
          var p1 = approx[i % 4], p2 = approx[i - 2], p0 = approx[i - 1];
          var dx1 = p1[0] - p0[0], dy1 = p1[1] - p0[1], dx2 = p2[0] - p0[0], dy2 = p2[1] - p0[1];
          var cos = (dx1 * dx2 + dy1 * dy2) / Math.sqrt((dx1 * dx1 + dy1 * dy1) * (dx2 * dx2 + dy2 * dy2) + 1e-10);
          if (Math.abs(cos) >= MAX_COSINE_THRESHOLD) ok = false;
        }
        if (ok) return approx;
      }
      return null;
    }
    // ImageUtils.four_point_transform
    function fourPointTransform(image, pts) {
      var r = orderPoints(pts), tl = r[0], tr = r[1], br = r[2], bl = r[3];
      var maxW = Math.max(Math.trunc(Math.hypot(br[0] - bl[0], br[1] - bl[1])), Math.trunc(Math.hypot(tr[0] - tl[0], tr[1] - tl[1])));
      var maxH = Math.max(Math.trunc(Math.hypot(tr[0] - br[0], tr[1] - br[1])), Math.trunc(Math.hypot(tl[0] - bl[0], tl[1] - bl[1])));
      if (maxW < 2 || maxH < 2) return null;
      var H = getPerspectiveTransform([[0, 0], [maxW - 1, 0], [maxW - 1, maxH - 1], [0, maxH - 1]], r);
      return warpPerspective(image, H, maxW, maxH, 0);
    }

    // CropOnMarkers: four printed corner markers found by normalised cross-correlation.
    // Python runs cv2.matchTemplate at full processing resolution for every scale; this
    // port searches a downsampled pyramid level first and refines around the best hits.
    function CropOnMarkers(options, marker, config) {
      this.name = "CropOnMarkers";
      this.needsFullResolution = false;
      this.geometry = "recorded";
      this.minMatchingThreshold = options.min_matching_threshold !== undefined ? options.min_matching_threshold : 0.3;
      this.maxMatchingVariation = options.max_matching_variation !== undefined ? options.max_matching_variation : 0.41;
      var range = options.marker_rescale_range || [35, 100];
      this.rescaleRange = [Math.trunc(range[0]), Math.trunc(range[1])];
      this.rescaleSteps = Math.trunc(options.marker_rescale_steps !== undefined ? options.marker_rescale_steps : 10);
      this.applyErodeSubtract = options.apply_erode_subtract !== undefined ? !!options.apply_erode_subtract : true;
      if (!marker) throw new Error("CropOnMarkers needs the marker image '" + (options.relativePath || "omr_marker.jpg") + "' (pass it via options.assets or assetsBaseUrl)");
      if (options.sheetToMarkerWidthRatio) {
        var uw = config.dimensions.processing_width / Math.trunc(options.sheetToMarkerWidthRatio);
        marker = resizeLinear(marker, Math.trunc(uw), Math.trunc((marker.height * uw) / marker.width));
      }
      marker = normalizeMinMax(gaussianBlur(marker, 5, 5, 0));
      if (this.applyErodeSubtract) marker = subtractImages(marker, rankFilter(marker, 10, 10, false));
      this.marker = marker;
    }
    function subtractImages(a, b) {
      var out = new Uint8Array(a.data.length);
      for (var i = 0; i < out.length; i++) out[i] = (a.data[i] - b.data[i]) & 255;
      return makeImage(a.width, a.height, out);
    }
    function integralImages(img) {
      var w = img.width, h = img.height, d = img.data, W = w + 1;
      var s = new Float64Array(W * (h + 1)), s2 = new Float64Array(W * (h + 1));
      for (var y = 0; y < h; y++) {
        var rs = 0, rs2 = 0;
        for (var x = 0; x < w; x++) {
          var v = d[y * w + x];
          rs += v;
          rs2 += v * v;
          s[(y + 1) * W + x + 1] = s[y * W + x + 1] + rs;
          s2[(y + 1) * W + x + 1] = s2[y * W + x + 1] + rs2;
        }
      }
      return { s: s, s2: s2, W: W };
    }
    // TM_CCOEFF_NORMED for every template position with top-left in [x0,x1]x[y0,y1].
    // Returns {scores, w, h, x0, y0, best:{score,x,y}}.
    function nccMap(img, ii, tpl, x0, y0, x1, y1) {
      var tw = tpl.width, th = tpl.height, n = tw * th, td = tpl.data, mean = 0, i;
      for (i = 0; i < n; i++) mean += td[i];
      mean /= n;
      var tz = new Float64Array(n), tnorm = 0;
      for (i = 0; i < n; i++) {
        tz[i] = td[i] - mean;
        tnorm += tz[i] * tz[i];
      }
      x0 = Math.max(0, x0);
      y0 = Math.max(0, y0);
      x1 = Math.min(img.width - tw, x1);
      y1 = Math.min(img.height - th, y1);
      var mw = Math.max(0, x1 - x0 + 1), mh = Math.max(0, y1 - y0 + 1);
      var scores = new Float32Array(mw * mh), d = img.data, w = img.width, W = ii.W, S = ii.s, S2 = ii.s2;
      var best = { score: -Infinity, x: -1, y: -1 };
      for (var y = y0; y <= y1; y++)
        for (var x = x0; x <= x1; x++) {
          var a = y * W + x, b = a + tw, c = (y + th) * W + x, e = c + tw;
          var ws = S[e] - S[b] - S[c] + S[a], ws2 = S2[e] - S2[b] - S2[c] + S2[a];
          var ivar = ws2 - (ws * ws) / n;
          var num = 0, k = 0;
          for (var ty = 0; ty < th; ty++) {
            var row = (y + ty) * w + x;
            for (var tx = 0; tx < tw; tx++) num += tz[k++] * d[row + tx];
          }
          var den = Math.sqrt(Math.max(ivar, 0) * tnorm);
          var score = den > 1e-6 ? num / den : 0;
          scores[(y - y0) * mw + (x - x0)] = score;
          if (score > best.score) best = { score: score, x: x, y: y };
        }
      return { scores: scores, w: mw, h: mh, x0: x0, y0: y0, best: best };
    }
    // Best TM_CCOEFF_NORMED match of tpl inside region [rx,ry,rw,rh] of the full image:
    // exhaustive on a downsampled pyramid level, then refined at full resolution.
    function matchTemplateBest(pyr, tpl, region) {
      var img = pyr.full, rx = region[0], ry = region[1], rw = region[2], rh = region[3];
      if (rw < tpl.width || rh < tpl.height) return { score: -1, x: rx, y: ry };
      var minSide = Math.min(tpl.width, tpl.height), f = minSide >= 10 ? Math.max(2, Math.round(minSide / 8)) : 1;
      if (f === 1) return nccMap(img, pyr.ii(1), tpl, rx, ry, rx + rw - tpl.width, ry + rh - tpl.height).best;
      var level = pyr.level(f);
      var stpl = resizeArea(tpl, Math.max(1, Math.round(tpl.width / f)), Math.max(1, Math.round(tpl.height / f)));
      var m = nccMap(level, pyr.ii(f), stpl, Math.ceil(rx / f), Math.ceil(ry / f), Math.floor((rx + rw - tpl.width) / f), Math.floor((ry + rh - tpl.height) / f));
      // coarse local maxima, best first; refine the strongest few at full resolution
      var sc = m.scores, mw = m.w, mh = m.h, peaks = [];
      for (var yy = 0; yy < mh; yy++)
        for (var xx = 0; xx < mw; xx++) {
          var v = sc[yy * mw + xx], isPeak = true;
          for (var dy = -1; dy <= 1 && isPeak; dy++)
            for (var dx = -1; dx <= 1; dx++) {
              var nx = xx + dx, ny = yy + dy;
              if ((dx || dy) && nx >= 0 && ny >= 0 && nx < mw && ny < mh && sc[ny * mw + nx] > v) {
                isPeak = false;
                break;
              }
            }
          if (isPeak) peaks.push([v, xx + m.x0, yy + m.y0]);
        }
      peaks.sort(function (a, b) {
        return b[0] - a[0];
      });
      var best = { score: -Infinity, x: -1, y: -1 }, fullIi = pyr.ii(1);
      for (var k = 0; k < peaks.length && k < 10; k++) {
        var cx = peaks[k][1], cy = peaks[k][2];
        var r = nccMap(img, fullIi, tpl, Math.max(rx, cx * f - f), Math.max(ry, cy * f - f), Math.min(rx + rw - tpl.width, cx * f + f), Math.min(ry + rh - tpl.height, cy * f + f)).best;
        if (r.score > best.score) best = r;
      }
      return best;
    }
    function makePyramid(full) {
      var levels = { 1: full }, iis = {};
      return {
        full: full,
        level: function (f) {
          if (!levels[f]) levels[f] = resizeArea(full, Math.max(1, Math.round(full.width / f)), Math.max(1, Math.round(full.height / f)));
          return levels[f];
        },
        ii: function (f) {
          if (!iis[f]) iis[f] = integralImages(this.level(f));
          return iis[f];
        },
      };
    }
    CropOnMarkers.prototype.apply = function (image, ctx) {
      var w1 = image.width, h1 = image.height;
      var sub = this.applyErodeSubtract ? normalizeMinMax(subtractImages(image, rankFilter(image, 10, 10, false))) : cloneImage(image);
      var midh = Math.floor(h1 / 3), midw = Math.floor(w1 / 2);
      for (var y = 0; y < h1; y++)
        for (var x = midw; x < Math.min(midw + 2, w1); x++) sub.data[y * w1 + x] = 255;
      for (var y2 = midh; y2 < Math.min(midh + 2, h1); y2++) sub.data.fill(255, y2 * w1, (y2 + 1) * w1);
      var pyr = makePyramid(sub);
      var descent = Math.floor((this.rescaleRange[1] - this.rescaleRange[0]) / this.rescaleSteps) || 1;
      var bestScale = null, allMax = 0;
      for (var r0 = this.rescaleRange[1]; r0 > this.rescaleRange[0]; r0 -= descent) {
        var s = r0 / 100;
        if (!s) continue;
        var mh = Math.trunc(this.marker.height * s), mw = Math.trunc((this.marker.width * mh) / this.marker.height);
        if (mh < 2 || mw < 2) continue;
        var tpl = resizeLinear(this.marker, mw, mh);
        var res = matchTemplateBest(pyr, tpl, [0, 0, w1, h1]);
        if (allMax < res.score) {
          allMax = res.score;
          bestScale = s;
        }
      }
      ctx.registration = { method: "markers" };
      if (bestScale === null) {
        ctx.registration.error = "No marker matches for the given scale range";
        return null;
      }
      var oh = Math.trunc(this.marker.height * bestScale), ow = Math.trunc((this.marker.width * oh) / this.marker.height);
      var optimal = resizeLinear(this.marker, ow, oh);
      var quads = [[0, 0, midw, midh], [midw, 0, w1 - midw, midh], [0, midh, midw, h1 - midh], [midw, midh, w1 - midw, h1 - midh]];
      var centres = [], scores = [];
      for (var k = 0; k < 4; k++) {
        var m = matchTemplateBest(pyr, optimal, quads[k]);
        scores.push(roundTo(m.score, 3));
        if (m.score < this.minMatchingThreshold || Math.abs(allMax - m.score) >= this.maxMatchingVariation) {
          ctx.registration.error = "Marker not found in quadrant " + (k + 1) + " (score " + m.score.toFixed(3) + ")";
          return null;
        }
        centres.push([m.x + ow / 2, m.y + oh / 2]);
      }
      if (!plausibleQuad(centres, w1 * h1)) {
        ctx.registration.error = "Marker positions don't form a plausible page rectangle";
        return null;
      }
      ctx.registration = { method: "markers", scale: bestScale, scores: scores, corners: centres };
      recordGeometry(ctx, function (im) {
        return fourPointTransform(im, centres);
      });
      return fourPointTransform(image, centres);
    };
    function plausibleQuad(centres, imageArea) {
      var q = orderPoints(centres);
      // convexity
      var sign = 0;
      for (var i = 0; i < 4; i++) {
        var a = q[i], b = q[(i + 1) % 4], c = q[(i + 2) % 4];
        var cr = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]);
        if (cr !== 0) {
          if (sign && Math.sign(cr) !== sign) return false;
          sign = Math.sign(cr);
        }
      }
      if (polygonArea(q) < 0.2 * imageArea) return false;
      function len(a, b) {
        return Math.hypot(a[0] - b[0], a[1] - b[1]);
      }
      var top = len(q[1], q[0]), bottom = len(q[2], q[3]), left = len(q[3], q[0]), right = len(q[2], q[1]);
      return Math.max(top, bottom) / Math.max(Math.min(top, bottom), 1e-6) <= 1.5 && Math.max(left, right) / Math.max(Math.min(left, right), 1e-6) <= 1.5;
    }

    function LevelsProcessor(options) {
      this.name = "Levels";
      this.needsFullResolution = false;
      this.geometry = "none";
      var low = Math.trunc(255 * (options.low !== undefined ? options.low : 0)), high = Math.trunc(255 * (options.high !== undefined ? options.high : 1)), gamma = options.gamma !== undefined ? options.gamma : 1.0;
      var lut = new Uint8Array(256);
      for (var i = 0; i < 256; i++) {
        var v = i <= low ? 0 : i >= high ? 255 : Math.pow((i - low) / (high - low), 1 / gamma) * 255;
        lut[i] = Math.trunc(v);
      }
      this.lut = lut;
    }
    LevelsProcessor.prototype.apply = function (image) {
      return applyLut(image, this.lut);
    };
    function MedianBlurProcessor(options) {
      this.name = "MedianBlur";
      this.needsFullResolution = false;
      this.geometry = "none";
      this.k = Math.trunc(options.kSize || 5);
    }
    MedianBlurProcessor.prototype.apply = function (image) {
      return medianBlur(image, this.k);
    };
    function GaussianBlurProcessor(options) {
      this.name = "GaussianBlur";
      this.needsFullResolution = false;
      this.geometry = "none";
      var k = options.kSize || [3, 3];
      this.k = [Math.trunc(k[0]), Math.trunc(k[1])];
      this.sigma = Math.trunc(options.sigmaX || 0);
    }
    GaussianBlurProcessor.prototype.apply = function (image) {
      return gaussianBlur(image, this.k[0], this.k[1], this.sigma);
    };

    // ------------------------------------------------------------------------
    // Bubble reading (port of ImageInstanceOps.read_omr_response_detailed)
    // ------------------------------------------------------------------------
    function sortedNumbers(arr) {
      return Array.prototype.slice.call(arr).sort(function (a, b) {
        return a - b;
      });
    }
    function otsuThreshold1D(values, minSeparation) {
      if (minSeparation === undefined) minSeparation = 30;
      var v = sortedNumbers(values), n = v.length;
      var cum = new Float64Array(n), s = 0;
      for (var i = 0; i < n; i++) {
        s += v[i];
        cum[i] = s;
      }
      var total = cum[n - 1], bestScore = -1, bestIndex = null;
      for (var j = 1; j < n; j++) {
        var w0 = j / n, w1 = (n - j) / n, m0 = cum[j - 1] / j, m1 = (total - cum[j - 1]) / (n - j);
        var score = w0 * w1 * (m0 - m1) * (m0 - m1);
        if (score > bestScore) {
          bestScore = score;
          bestIndex = j;
        }
      }
      if (bestIndex === null) return null;
      var mean0 = 0, mean1 = 0;
      for (var a = 0; a < bestIndex; a++) mean0 += v[a];
      for (var b = bestIndex; b < n; b++) mean1 += v[b];
      mean0 /= bestIndex;
      mean1 /= n - bestIndex;
      if (mean1 - mean0 < minSeparation) return null;
      return (v[bestIndex - 1] + v[bestIndex]) / 2;
    }
    function getGlobalThreshold(values, tp, looseness) {
      looseness = looseness || 1;
      var def = tp.PAGE_TYPE_FOR_THRESHOLD === "white" ? GLOBAL_PAGE_THRESHOLD_WHITE : GLOBAL_PAGE_THRESHOLD_BLACK;
      var q = sortedNumbers(values);
      var ls = Math.floor((looseness + 1) / 2), l = q.length - ls;
      var max1 = tp.MIN_JUMP, thr1 = def;
      for (var i = ls; i < l; i++) {
        var jump = q[i + ls] - q[i - ls];
        if (jump > max1) {
          max1 = jump;
          thr1 = q[i - ls] + jump / 2;
        }
      }
      if (max1 === tp.MIN_JUMP && q.length >= 4) {
        var o = otsuThreshold1D(q);
        if (o !== null) thr1 = o;
      }
      return thr1;
    }
    function getLocalThreshold(values, globalThr, noOutliers, tp) {
      var q = sortedNumbers(values), lowConfidence = false, thr1;
      if (q.length < 3) {
        var mx = Math.max.apply(null, q), mn = Math.min.apply(null, q);
        if (mx - mn < tp.MIN_GAP) thr1 = globalThr;
        else
          thr1 =
            q.reduce(function (a, b) {
              return a + b;
            }, 0) / q.length;
      } else {
        var l = q.length - 1, max1 = tp.MIN_JUMP;
        thr1 = 255;
        for (var i = 1; i < l; i++) {
          var jump = q[i + 1] - q[i - 1];
          if (jump > max1) {
            max1 = jump;
            thr1 = q[i - 1] + jump / 2;
          }
        }
        if (max1 < tp.MIN_JUMP + tp.CONFIDENT_SURPLUS) {
          if (noOutliers) thr1 = globalThr;
          else lowConfidence = true;
        }
      }
      return { threshold: thr1, lowConfidence: lowConfidence };
    }
    function boxMean(img, x, y, w, h) {
      var x0 = Math.max(x, 0), y0 = Math.max(y, 0), x1 = Math.min(x + w, img.width), y1 = Math.min(y + h, img.height);
      if (x1 <= x0 || y1 <= y0) return 0;
      var s = 0, d = img.data, W = img.width;
      for (var yy = y0; yy < y1; yy++) {
        var b = yy * W;
        for (var xx = x0; xx < x1; xx++) s += d[b + xx];
      }
      return s / ((x1 - x0) * (y1 - y0));
    }
    var maskCache = {};
    function fillRatio(img, x, y, w, h, threshold) {
      var x0 = Math.max(x, 0), y0 = Math.max(y, 0), x1 = Math.min(x + w, img.width), y1 = Math.min(y + h, img.height);
      var rw = x1 - x0, rh = y1 - y0;
      if (rw <= 0 || rh <= 0) return 0.0;
      var key = rw + "x" + rh, mask = maskCache[key];
      if (!mask) {
        mask = ellipseMask(rw, rh, rw >> 1, rh >> 1, Math.max(Math.trunc(rw * 0.35), 1), Math.max(Math.trunc(rh * 0.35), 1));
        maskCache[key] = mask;
      }
      var inside = 0, dark = 0, d = img.data, W = img.width;
      for (var yy = 0; yy < rh; yy++)
        for (var xx = 0; xx < rw; xx++) {
          if (!mask[yy * rw + xx]) continue;
          inside++;
          if (d[(y0 + yy) * W + x0 + xx] < threshold) dark++;
        }
      return inside ? dark / inside : 0.0;
    }
    function stdPop(arr) {
      var n = arr.length, m = 0;
      for (var i = 0; i < n; i++) m += arr[i];
      m /= n;
      var s = 0;
      for (var j = 0; j < n; j++) s += (arr[j] - m) * (arr[j] - m);
      return Math.sqrt(s / n);
    }
    function summarizeField(label, value, bubbles, markedCount, lowConfidence, params, extraFlags) {
      var flags = {};
      (extraFlags || []).forEach(function (f) {
        flags[f] = 1;
      });
      if (markedCount > 1) flags.multi_marked = 1;
      if (markedCount === 0) flags.empty = 1;
      if (lowConfidence) flags.ambiguous_threshold = 1;
      var confidence = bubbles.length ? Infinity : 0.0;
      bubbles.forEach(function (b) {
        if (b.marked && b.fill_ratio < params.min_marked_fill_ratio) flags.weak_mark = 1;
        if (!b.marked && b.fill_ratio > params.max_unmarked_fill_ratio) flags.possible_missed_mark = 1;
        if (b.model_disagrees) flags.model_disagrees = 1;
        confidence = Math.min(confidence, b.confidence);
      });
      if (confidence < params.min_confidence) flags.low_confidence = 1;
      var list = Object.keys(flags).sort();
      return {
        label: label,
        value: value,
        confidence: roundTo(confidence, 3),
        flags: list,
        needs_review: list.some(function (f) {
          return params.review_flags.indexOf(f) >= 0;
        }),
        bubbles: bubbles,
      };
    }

    // Snap each field block onto its printed bubble outlines (alignment_params.block_snap_radius)
    function snapFieldBlock(img, block, radius) {
      var bw = block.bubbleDimensions[0], bh = block.bubbleDimensions[1];
      var blockW = Math.trunc(block.dimensions[0]), blockH = Math.trunc(block.dimensions[1]);
      var x0 = block.origin[0], y0 = block.origin[1];
      var left = x0 - radius, top = y0 - radius, right = x0 + blockW + radius, bottom = y0 + blockH + radius;
      if (left < 0 || top < 0 || right > img.width || bottom > img.height) return [block.shift, 0];
      // every bubble outline, drawn like cv2.ellipse(..., thickness=2)
      var axW = Math.max(Math.trunc(bw / 2) - 1, 1), axH = Math.max(Math.trunc(bh / 2) - 1, 1);
      var outline = new Uint8Array(blockW * blockH);
      block.fields.forEach(function (f) {
        f.bubbles.forEach(function (b) {
          ellipseOutline(outline, blockW, blockH, Math.trunc(b.x - x0 + bw / 2), Math.trunc(b.y - y0 + bh / 2), axW, axH);
        });
      });
      var pts = [];
      for (var pi = 0; pi < outline.length; pi++) if (outline[pi]) pts.push(pi);
      var n = blockW * blockH, k = pts.length;
      if (!k || k === n) return [block.shift, 0];
      // TM_CCOEFF_NORMED of a binary mask against (255 - region)
      var W = img.width, d = img.data, size = 2 * radius + 1;
      var tMean = k / n, tNorm = k * (1 - tMean) * (1 - tMean) + (n - k) * tMean * tMean;
      var regW = blockW + 2 * radius, regH = blockH + 2 * radius;
      var region = makeImage(regW, regH);
      for (var ry = 0; ry < regH; ry++) for (var rx = 0; rx < regW; rx++) region.data[ry * regW + rx] = 255 - d[(top + ry) * W + left + rx];
      var ii = integralImages(region), IW = ii.W;
      var ptX = new Int32Array(k), ptY = new Int32Array(k);
      for (var p = 0; p < k; p++) {
        ptX[p] = pts[p] % blockW;
        ptY[p] = Math.floor(pts[p] / blockW);
      }
      var scores = new Float64Array(size * size), best = -Infinity, bx = radius, by = radius;
      for (var oy = 0; oy < size; oy++)
        for (var ox = 0; ox < size; ox++) {
          var a = oy * IW + ox, b = oy * IW + ox + blockW, c = (oy + blockH) * IW + ox, e = (oy + blockH) * IW + ox + blockW;
          var ws = ii.s[e] - ii.s[b] - ii.s[c] + ii.s[a], ws2 = ii.s2[e] - ii.s2[b] - ii.s2[c] + ii.s2[a];
          var on = 0;
          for (var q = 0; q < k; q++) on += region.data[(oy + ptY[q]) * regW + ox + ptX[q]];
          var num = on - tMean * ws, den = Math.sqrt(Math.max(ws2 - (ws * ws) / n, 0) * tNorm);
          var sc = den > 1e-9 ? num / den : 0;
          scores[oy * size + ox] = sc;
          if (sc > best) {
            best = sc;
            bx = ox;
            by = oy;
          }
        }
      // only move when clearly better, and never to the edge of the search window
      if (best - scores[radius * size + radius] < 0.02 || Math.abs(bx - radius) === radius || Math.abs(by - radius) === radius) return [block.shift, 0];
      return [bx - radius, by - radius];
    }

    // numpy's add.reduce over a contiguous 1-D array (pairwise summation); f32: float32 maths
    function pairwiseSum(a, s, n, f32) {
      var i, res, j;
      if (n < 8) {
        res = 0;
        for (i = 0; i < n; i++) res = f32 ? fr(res + a[s + i]) : res + a[s + i];
        return res;
      }
      if (n <= 128) {
        var r = [];
        for (j = 0; j < 8; j++) r.push(a[s + j]);
        for (i = 8; i < n - (n % 8); i += 8) for (j = 0; j < 8; j++) r[j] = f32 ? fr(r[j] + a[s + i + j]) : r[j] + a[s + i + j];
        if (f32) res = fr(fr(fr(r[0] + r[1]) + fr(r[2] + r[3])) + fr(fr(r[4] + r[5]) + fr(r[6] + r[7])));
        else res = r[0] + r[1] + (r[2] + r[3]) + (r[4] + r[5] + (r[6] + r[7]));
        for (; i < n; i++) res = f32 ? fr(res + a[s + i]) : res + a[s + i];
        return res;
      }
      var n2 = Math.floor(n / 2);
      n2 -= n2 % 8;
      var t = pairwiseSum(a, s, n2, f32) + pairwiseSum(a, s + n2, n - n2, f32);
      return f32 ? fr(t) : t;
    }
    function npSum(a, f32) {
      return pairwiseSum(a, 0, a.length, f32);
    }

    // ------------------------------------------------------------------------
    // Field block rectification onto a printed border (port of src/rectify.py)
    // ------------------------------------------------------------------------
    var RECT_MIN_SEGMENT_SHARE = 0.6, RECT_MIN_COVERAGE = 0.5, RECT_MAX_LINE_RMS = 1.5, RECT_MAX_ANGLE_DEV = 6.0, RECT_MAX_SIDE_RATIO_DEV = 0.08, RECT_MIN_FIT_RATIO = 0.97;
    function blockBubbles(block) {
      var out = [];
      block.fields.forEach(function (f) {
        f.bubbles.forEach(function (b) {
          out.push(b);
        });
      });
      return out;
    }
    // np.polyfit(x, y, 1) on float32 points: lstsq in double, float32 result
    function polyfitF32(xs, ys) {
      var n = xs.length, s0 = 0, i;
      for (i = 0; i < n; i++) s0 = fr(s0 + fr(xs[i] * xs[i]));
      var sc0 = fr(Math.sqrt(s0)), sc1 = fr(Math.sqrt(n));
      var u = new Float64Array(n), v = fr(1 / sc1);
      for (i = 0; i < n; i++) u[i] = fr(xs[i] / sc0);
      // least squares [u, v] c = y via Gram-Schmidt in double
      var vv = n * v * v, uv = 0, uy = 0, vy = 0;
      for (i = 0; i < n; i++) {
        uv += u[i] * v;
        vy += v * ys[i];
      }
      var k = uv / vv, rr = 0;
      for (i = 0; i < n; i++) {
        var ru = u[i] - k * v;
        rr += ru * ru;
        uy += ru * ys[i];
      }
      var c0 = uy / rr, c1 = vy / vv - k * c0;
      return [fr(fr(c0) / sc0), fr(fr(c1) / sc1)];
    }
    // _fit_side: a near-horizontal line (in mask's orientation) close to `expected`
    function fitSide(mask, mw, mh, transposed, along, expected, search) {
      var rows = transposed ? mw : mh, cols = transposed ? mh : mw;
      function at(r, c) {
        return transposed ? mask[c * mw + r] : mask[r * mw + c];
      }
      var lo = Math.trunc(Math.max(0, Math.floor(expected - search))), hi = Math.trunc(Math.min(rows, Math.ceil(expected + search) + 1));
      var start = roundHalfEven(along[0]), end = roundHalfEven(along[1]);
      if (hi - lo < 3 || end - start < 20) return null;
      var count = clamp(Math.floor((end - start) / 40), 4, 12);
      var edges = [], i, r, c;
      for (i = 0; i <= count; i++) edges.push(Math.trunc(i === count ? end : start + i * ((end - start) / count)));
      var px = [], py = [];
      for (var e = 0; e < count; e++) {
        var a = edges[e], b = edges[e + 1];
        if (b <= a) continue;
        var cov = new Float64Array(hi - lo), peak = 0;
        for (r = lo; r < hi; r++) {
          var s = 0;
          for (c = a; c < b && c < cols; c++) s += at(r, c) ? 1 : 0;
          cov[r - lo] = s / (Math.min(b, cols) - a);
          if (cov[r - lo] > cov[peak]) peak = r - lo;
        }
        if (cov[peak] < RECT_MIN_COVERAGE) continue;
        var thr = Math.max(RECT_MIN_COVERAGE, 0.8 * cov[peak]), groups = [], cur = null;
        for (r = 0; r < cov.length; r++) {
          if (cov[r] >= thr) {
            if (cur && r === cur[cur.length - 1] + 1) cur.push(r);
            else groups.push((cur = [r]));
          }
        }
        var best = null, bestD = Infinity;
        groups.forEach(function (g) {
          var m = 0;
          g.forEach(function (x) {
            m += x;
          });
          var d = Math.abs(lo + m / g.length - expected);
          if (d < bestD) {
            bestD = d;
            best = g;
          }
        });
        var bw = best.map(function (x) {
          return x * cov[x];
        }),
          ws = best.map(function (x) {
            return cov[x];
          });
        px.push(fr((a + b) / 2.0));
        py.push(fr(lo + npSum(bw, false) / npSum(ws, false)));
      }
      var needed = Math.max(3, Math.ceil(RECT_MIN_SEGMENT_SHARE * count));
      if (px.length < needed) return null;
      var keep = px.map(function () {
        return true;
      });
      function fitKept() {
        var kx = [], ky = [];
        for (var q = 0; q < px.length; q++)
          if (keep[q]) {
            kx.push(px[q]);
            ky.push(py[q]);
          }
        return polyfitF32(kx, ky);
      }
      var line, q, kept;
      for (var it = 0; it < 2; it++) {
        line = fitKept();
        kept = 0;
        for (q = 0; q < px.length; q++) {
          keep[q] = Math.abs(fr(py[q] - fr(fr(line[0] * px[q]) + line[1]))) <= 2.0;
          if (keep[q]) kept++;
        }
        if (kept < needed) return null;
      }
      line = fitKept();
      var sq = [];
      for (q = 0; q < px.length; q++)
        if (keep[q]) {
          var res = fr(py[q] - fr(fr(line[0] * px[q]) + line[1]));
          sq.push(fr(res * res));
        }
      if (fr(Math.sqrt(fr(npSum(sq, true) / sq.length))) > RECT_MAX_LINE_RMS) return null;
      if (Math.abs(line[0]) > Math.tan((RECT_MAX_ANGLE_DEV * 2 * Math.PI) / 180)) return null;
      return line;
    }
    function lineAt(line, t) {
      return line[0] * t + line[1];
    }
    function intersectLines(hz, vt) {
      var y = (hz[0] * vt[1] + hz[1]) / (1.0 - hz[0] * vt[0]);
      return [vt[0] * y + vt[1], y];
    }
    function nearRectangular(c) {
      for (var i = 0; i < 4; i++) {
        var pp = c[(i + 3) % 4], p = c[i], pn = c[(i + 1) % 4];
        var ux = fr(pp[0] - p[0]), uy = fr(pp[1] - p[1]), vx = fr(pn[0] - p[0]), vy = fr(pn[1] - p[1]);
        var cos = (ux * vx + uy * vy) / (Math.hypot(ux, uy) * Math.hypot(vx, vy) + 1e-9);
        if (Math.abs((Math.acos(clamp(cos, -1, 1)) * 180) / Math.PI - 90) > RECT_MAX_ANGLE_DEV) return false;
      }
      var L = [];
      for (var k = 0; k < 4; k++) L.push(Math.hypot(c[(k + 1) % 4][0] - c[k][0], c[(k + 1) % 4][1] - c[k][1]));
      return !(Math.abs(L[0] - L[2]) > RECT_MAX_SIDE_RATIO_DEV * Math.max(L[0], L[2]) || Math.abs(L[1] - L[3]) > RECT_MAX_SIDE_RATIO_DEV * Math.max(L[1], L[3]));
    }
    // Mean darkness on the expected bubble outlines at the given positions
    function bubbleFit(img, bubbles, bw, bh, offsets) {
      var cs = bubbles.map(function (b, i) {
        return [Math.trunc(b.x + offsets[i][0] + bw / 2), Math.trunc(b.y + offsets[i][1] + bh / 2)];
      });
      var pad = Math.trunc(Math.max(bw, bh)), minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      cs.forEach(function (c) {
        minX = Math.min(minX, c[0]);
        maxX = Math.max(maxX, c[0]);
        minY = Math.min(minY, c[1]);
        maxY = Math.max(maxY, c[1]);
      });
      var left = Math.max(minX - pad, 0), top = Math.max(minY - pad, 0), right = Math.min(maxX + pad, img.width), bottom = Math.min(maxY + pad, img.height);
      if (right <= left || bottom <= top) return 0.0;
      var w = right - left, h = bottom - top, mask = new Uint8Array(w * h);
      var axW = Math.max(Math.trunc(bw / 2) - 1, 1), axH = Math.max(Math.trunc(bh / 2) - 1, 1);
      cs.forEach(function (c) {
        ellipseOutline(mask, w, h, c[0] - left, c[1] - top, axW, axH);
      });
      var s = 0, n = 0;
      for (var y = 0; y < h; y++)
        for (var x = 0; x < w; x++)
          if (mask[y * w + x]) {
            s += img.data[(top + y) * img.width + left + x];
            n++;
          }
      return n ? 255.0 - s / n : 0.0;
    }
    // rectify_field_block: {ok, reason, offsets?, maxShift?}
    function rectifyFieldBlock(img, block, searchPx) {
      var x0 = Number(block.origin[0]), y0 = Number(block.origin[1]);
      var x1 = x0 + Number(block.dimensions[0]), y1 = y0 + Number(block.dimensions[1]);
      var pad = block.borderPadding;
      var padding = pad === null || pad === undefined ? null : typeof pad === "number" ? [pad, pad] : [Number(pad[0]), Number(pad[1])];
      var search = Number(searchPx);
      var cx = padding ? padding[0] : search, cy = padding ? padding[1] : search;
      var margin = Math.ceil(Math.max(cx, cy) + search + 3);
      var left = Math.trunc(x0) - margin, top = Math.trunc(y0) - margin, right = Math.ceil(x1) + margin, bottom = Math.ceil(y1) + margin;
      if (left < 0 || top < 0 || right > img.width || bottom > img.height) return { ok: false, reason: "search window outside the page" };
      var region = cropImage(img, left, top, right - left, bottom - top), rw = region.width, rh = region.height;
      var t = otsuValue(region), dark = new Uint8Array(rw * rh);
      for (var i = 0; i < dark.length; i++) dark[i] = region.data[i] > t ? 0 : 255;
      var darkImg = makeImage(rw, rh, dark), bw = block.bubbleDimensions[0], bh = block.bubbleDimensions[1];
      var run = Math.trunc(Math.max(bw, bh) * 1.5) + 1;
      var horizontal = morphBinary(morphBinary(morphBinary(darkImg, 1, 3, true), run, 1, false), run, 1, true).data;
      var vertical = morphBinary(morphBinary(morphBinary(darkImg, 3, 1, true), 1, run, false), 1, run, true).data;
      var loX = x0 - left, hiX = x1 - left, loY = y0 - top, hiY = y1 - top, sides = {};
      var specs = [
        ["top", horizontal, false, [loX, hiX], loY - cy],
        ["bottom", horizontal, false, [loX, hiX], hiY + cy],
        ["left", vertical, true, [loY, hiY], loX - cx],
        ["right", vertical, true, [loY, hiY], hiX + cx],
      ];
      for (var s = 0; s < 4; s++) {
        var line = fitSide(specs[s][1], rw, rh, specs[s][2], specs[s][3], specs[s][4], search);
        if (!line) return { ok: false, reason: specs[s][0] + " border not found" };
        sides[specs[s][0]] = line;
      }
      var corners = [intersectLines(sides.top, sides.left), intersectLines(sides.top, sides.right), intersectLines(sides.bottom, sides.right), intersectLines(sides.bottom, sides.left)].map(function (p) {
        return [fr(fr(p[0]) + left), fr(fr(p[1]) + top)];
      });
      var padX, padY;
      if (!padding) {
        var midY = (loY + hiY) / 2, midX = (loX + hiX) / 2;
        padX = (loX - lineAt(sides.left, midY) + (lineAt(sides.right, midY) - hiX)) / 2;
        padY = (loY - lineAt(sides.top, midX) + (lineAt(sides.bottom, midX) - hiY)) / 2;
        if (Math.min(padX, padY) < -2 || Math.max(padX, padY) > 2 * search + 2) return { ok: false, reason: "border gap implausible" };
      } else {
        padX = padding[0];
        padY = padding[1];
      }
      var expected = [[x0 - padX, y0 - padY], [x1 + padX, y0 - padY], [x1 + padX, y1 + padY], [x0 - padX, y1 + padY]].map(function (p) {
        return [fr(p[0]), fr(p[1])];
      });
      var maxShift = 0;
      for (var k = 0; k < 4; k++) {
        var ddx = fr(corners[k][0] - expected[k][0]), ddy = fr(corners[k][1] - expected[k][1]);
        maxShift = Math.max(maxShift, fr(Math.sqrt(fr(fr(ddx * ddx) + fr(ddy * ddy)))));
      }
      var out = { ok: false, reason: "", maxShift: maxShift };
      if (maxShift > search) {
        out.reason = "correction larger than the search margin";
        return out;
      }
      if (!nearRectangular(corners)) {
        out.reason = "border is not near-rectangular";
        return out;
      }
      var H = getPerspectiveTransform(expected, corners);
      var bubbles = blockBubbles(block);
      var offsets = bubbles.map(function (b) {
        var ccx = fr(b.x + bw / 2.0), ccy = fr(b.y + bh / 2.0), m = projectPoint(H, ccx, ccy);
        return [roundHalfEven(fr(fr(m[0]) - ccx)), roundHalfEven(fr(fr(m[1]) - ccy))];
      });
      var shift = bubbles.map(function () {
        return [block.shift, block.shiftY];
      });
      if (bubbleFit(img, bubbles, bw, bh, offsets) < bubbleFit(img, bubbles, bw, bh, shift) * RECT_MIN_FIT_RATIO) {
        out.reason = "bubbles fit worse after rectification";
        return out;
      }
      out.ok = true;
      out.offsets = offsets;
      return out;
    }
    // rectify_field_blocks: returns {block name: true} for blocks that failed
    function rectifyFieldBlocks(img, template, alignment) {
      var failed = {}, byDefault = !!alignment.rectify_on_border, search = alignment.rectify_search_px !== undefined ? alignment.rectify_search_px : 20;
      template.fieldBlocks.forEach(function (block) {
        if (block.rectified) {
          blockBubbles(block).forEach(function (b) {
            b.dx = b.dy = 0;
          });
          block.rectified = false;
        }
        var enabled = block.rectifyOnBorder;
        if (!(enabled === null || enabled === undefined ? byDefault : enabled)) return;
        var res = rectifyFieldBlock(img, block, search);
        block.lastRectification = res;
        if (res.ok) {
          blockBubbles(block).forEach(function (b, i) {
            b.dx = res.offsets[i][0];
            b.dy = res.offsets[i][1];
          });
          block.shift = 0;
          block.shiftY = 0;
          block.rectified = true;
        } else failed[block.name] = true;
      });
      return failed;
    }

    // Grey morphology with cv2.getStructuringElement(MORPH_ELLIPSE, (k, k)); outside pixels ignored
    function morphEllipse(img, k, isMax) {
      var w = img.width, h = img.height, r = k >> 1, out = new Uint8Array(w * h).fill(isMax ? 0 : 255), rows = {};
      for (var i = 0; i < k; i++) {
        var dy = i - r;
        if (Math.abs(dy) > r) continue;
        var dx = Math.round(r * Math.sqrt((r * r - dy * dy) / (r * r)));
        var j1 = Math.max(r - dx, 0), j2 = Math.min(r + dx + 1, k), key = j1 + ":" + j2;
        if (!rows[key]) rows[key] = rowExtreme(img.data, w, h, r - j1, j2 - 1 - r, isMax);
        var src = rows[key];
        for (var y = 0; y < h; y++) {
          var sy = y + dy;
          if (sy < 0 || sy >= h) continue;
          var o = y * w, so = sy * w;
          for (var x = 0; x < w; x++) {
            var v = src[so + x];
            if (isMax ? v > out[o + x] : v < out[o + x]) out[o + x] = v;
          }
        }
      }
      return makeImage(w, h, out);
    }
    // Port of ImageInstanceOps.flatten_background: the page divided by a smooth
    // estimate of its paper brightness (closing at quarter resolution, blurred)
    function flattenBackground(img, bubbleSize) {
      var w = img.width, h = img.height;
      var small = resizeArea(img, Math.max(Math.floor(w / 4), 1), Math.max(Math.floor(h / 4), 1));
      var k = Math.max(3, Math.round((3 * bubbleSize) / 4)) | 1;
      var bg = morphEllipse(morphEllipse(small, k, true), k, false);
      bg = resizeLinear(gaussianBlur(bg, k, k, 0), w, h);
      var out = new Uint8Array(w * h), d = img.data, b = bg.data;
      for (var i = 0; i < out.length; i++) {
        var q = (d[i] * 255) / Math.max(b[i], 1), f = Math.floor(q), v = q - f > 0.5 || (q - f === 0.5 && f % 2) ? f + 1 : f;
        out[i] = v > 255 ? 255 : v;
      }
      return makeImage(w, h, out);
    }

    function readBubbles(template, image, config, modelProbs) {
      var tp = config.threshold_params, rp = config.review_params, ap = config.alignment_params || {};
      var img = image;
      if (img.width !== template.pageDimensions[0] || img.height !== template.pageDimensions[1]) img = resizeLinear(img, template.pageDimensions[0], template.pageDimensions[1]);
      var mm = minMax(img);
      if (mm[1] > mm[0]) img = normalizeMinMax(img);
      // Zones and stored images use the page as aligned
      var alignedOut = img;
      if (tp.flatten_background && template.fieldBlocks.length) {
        var sizes = template.fieldBlocks.map(function (b) {
          return Math.max(b.bubbleDimensions[0], b.bubbleDimensions[1]);
        }).sort(function (a, b) {
          return a - b;
        });
        var mid = sizes.length >> 1;
        img = flattenBackground(img, sizes.length % 2 ? sizes[mid] : (sizes[mid - 1] + sizes[mid]) / 2);
      }
      var snap = ap.block_snap_radius || 0;
      template.fieldBlocks.forEach(function (block) {
        block.shiftY = 0;
        block.shift = 0;
        if (snap) {
          var s = snapFieldBlock(img, block, snap);
          block.shift = s[0];
          block.shiftY = s[1];
        }
      });
      var rectifyFailed = rectifyFieldBlocks(img, template, ap);
      var allVals = [], strips = [], stds = [];
      template.fieldBlocks.forEach(function (block) {
        var bw = block.bubbleDimensions[0], bh = block.bubbleDimensions[1];
        block.fields.forEach(function (field) {
          var vals = field.bubbles.map(function (b) {
            return boxMean(img, b.x + block.shift + b.dx, b.y + block.shiftY + b.dy, bw, bh);
          });
          stds.push(roundTo(stdPop(vals), 2));
          strips.push(vals);
          Array.prototype.push.apply(allVals, vals);
        });
      });
      // threshold_params.mode "fixed": one intensity line for every sheet
      var fixedMode = (tp.mode || "adaptive") === "fixed";
      var fixedThr = 0, fixedMinFill = 0, fillMargin = 0, globalStdThresh, globalThr;
      if (fixedMode) {
        fixedThr = Number(tp.fixed_threshold !== undefined ? tp.fixed_threshold : 120);
        fixedMinFill = Number(tp.fixed_min_fill_ratio !== undefined ? tp.fixed_min_fill_ratio : 0.12);
        fillMargin = Math.max(Math.min(fixedMinFill, 1.0 - fixedMinFill), 0.05);
        globalStdThresh = globalThr = fixedThr;
      } else {
        globalStdThresh = getGlobalThreshold(stds, tp, 1);
        globalThr = getGlobalThreshold(allVals, tp, 4);
      }
      var probs = modelProbs ? modelProbs(img, template) : null;
      return Promise.resolve(probs).then(function (modelMarkedProbs) {
        var omrResponse = {}, fieldDetails = {}, thresholdSum = 0, stripNo = 0, boxNo = 0;
        template.fieldBlocks.forEach(function (block) {
          var bw = block.bubbleDimensions[0], bh = block.bubbleDimensions[1];
          var extra = rectifyFailed[block.name] ? ["rectify_failed"] : null;
          block.fields.forEach(function (field) {
            var thr, lowConfidence;
            if (fixedMode) {
              thr = fixedThr;
              lowConfidence = false;
            } else {
              var local = getLocalThreshold(strips[stripNo], globalThr, stds[stripNo] < globalStdThresh, tp);
              thr = local.threshold;
              lowConfidence = local.lowConfidence;
            }
            thresholdSum += thr;
            var detected = [], details = [];
            field.bubbles.forEach(function (bubble, bi) {
              var mean = strips[stripNo][bi];
              var modelProb = modelMarkedProbs ? modelMarkedProbs[boxNo] : null;
              boxNo++;
              var x = bubble.x + block.shift + bubble.dx, y = bubble.y + block.shiftY + bubble.dy;
              var marked, fill, conf;
              if (fixedMode) {
                // marked when enough of the interior is darker than the line
                fill = fillRatio(img, x, y, bw, bh, fixedThr);
                marked = fill >= fixedMinFill;
                conf = clamp(Math.abs(fill - fixedMinFill) / fillMargin, 0, 1);
              } else {
                marked = thr > mean;
                fill = fillRatio(img, x, y, bw, bh, thr - rp.confidence_margin);
                conf = clamp(Math.abs(thr - mean) / rp.confidence_margin, 0, 1);
              }
              var detail = { value: bubble.value, x: x, y: y, w: bw, h: bh, mean_intensity: roundTo(mean, 2), fill_ratio: roundTo(fill, 3), marked: marked, confidence: roundTo(conf, 3) };
              if (modelProb !== null && modelProb !== undefined) {
                var modelMarked = modelProb >= 0.5;
                detail.model_marked_prob = roundTo(modelProb, 4);
                detail.model_disagrees = modelMarked !== marked;
                marked = modelMarked;
                detail.marked = modelMarked;
                detail.confidence = roundTo(Math.abs(modelProb - 0.5) * 2, 3);
              }
              details.push(detail);
              if (marked) detected.push(bubble);
            });
            detected.forEach(function (b) {
              omrResponse[b.label] = b.label in omrResponse ? omrResponse[b.label] + b.value : b.value;
            });
            if (!detected.length) omrResponse[field.label] = block.emptyValue;
            fieldDetails[field.label] = summarizeField(field.label, omrResponse[field.label], details, detected.length, lowConfidence, rp, extra);
            stripNo++;
          });
        });
        var thresholds = { global: roundTo(globalThr, 2), global_std: roundTo(globalStdThresh, 2), average_local: stripNo ? roundTo(thresholdSum / stripNo, 2) : 0 };
        if (fixedMode) thresholds.mode = "fixed";
        return {
          omrResponse: omrResponse,
          fieldDetails: fieldDetails,
          alignedImage: alignedOut,
          thresholds: thresholds,
        };
      });
    }

    // ------------------------------------------------------------------------
    // Built-in 1-D barcode decoder (port of src/readers/linear.py): Code 128,
    // Code 39, ITF, EAN-13/8 and UPC-A from scanline run lengths
    // ------------------------------------------------------------------------
    var LIN = { CODE128: "Code 128", CODE39: "Code 39", ITF: "ITF", EAN13: "EAN-13", EAN8: "EAN-8", UPCA: "UPC-A" };
    var LIN_ALL = [LIN.CODE128, LIN.CODE39, LIN.ITF, LIN.EAN13, LIN.EAN8, LIN.UPCA];
    var LIN_ALIASES = { code128: LIN.CODE128, code39: LIN.CODE39, itf: LIN.ITF, ean13: LIN.EAN13, ean8: LIN.EAN8, upca: LIN.UPCA, linearcodes: null, all: null, any: null };
    var C128_PATTERNS = (
      "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 " +
      "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 " +
      "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 " +
      "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 " +
      "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 " +
      "114131 311141 411131 211412 211214 211232"
    )
      .split(" ")
      .map(function (p) {
        return p.split("").map(Number);
      });
    var C128_STOP = [2, 3, 3, 1, 1, 1, 2];
    var C128_FNC1 = 102, C128_SHIFT = 98, C128_CODE_C = 99, C128_CODE_B = 100, C128_CODE_A = 101;
    var C39_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%";
    var C39_ENCODINGS = [0x034, 0x121, 0x061, 0x160, 0x031, 0x130, 0x070, 0x025, 0x124, 0x064, 0x109, 0x049, 0x148, 0x019, 0x118, 0x058, 0x00d, 0x10c, 0x04c, 0x01c, 0x103, 0x043, 0x142, 0x013, 0x112, 0x052, 0x007, 0x106, 0x046, 0x016, 0x181, 0x0c1, 0x1c0, 0x091, 0x190, 0x0d0, 0x085, 0x184, 0x0c4, 0x0a8, 0x0a2, 0x08a, 0x02a];
    var C39_BY_CODE = {};
    C39_ENCODINGS.forEach(function (code, i) {
      C39_BY_CODE[code] = C39_ALPHABET[i];
    });
    C39_BY_CODE[0x094] = "*";
    var ITF_PATTERNS = [[1, 1, 2, 2, 1], [2, 1, 1, 1, 2], [1, 2, 1, 1, 2], [2, 2, 1, 1, 1], [1, 1, 2, 1, 2], [2, 1, 2, 1, 1], [1, 2, 2, 1, 1], [1, 1, 1, 2, 2], [2, 1, 1, 2, 1], [1, 2, 1, 2, 1]];
    var EAN_L = [[3, 2, 1, 1], [2, 2, 2, 1], [2, 1, 2, 2], [1, 4, 1, 1], [1, 1, 3, 2], [1, 2, 3, 1], [1, 1, 1, 4], [1, 3, 1, 2], [1, 2, 1, 3], [3, 1, 1, 2]];
    var EAN_G = EAN_L.map(function (p) {
      return p.slice().reverse();
    });
    var EAN_LG = EAN_L.concat(EAN_G);
    var EAN_FIRST_DIGIT = { "000000": 0, "001011": 1, "001101": 2, "001110": 3, "010011": 4, "011001": 5, "011100": 6, "010101": 7, "010110": 8, "011010": 9 };
    // zxing >= 2.3 (and zxing-wasm) report a UPC-A as EAN-13 unless only UPC-A is allowed
    var UPCA_AS_EAN13 = true;
    var MAX_PATTERN_ERROR = 0.32, MIN_PATTERN_MARGIN = 0.12;

    // Map a zone's `formats` onto the symbologies read here ({} = none supported)
    function builtinFormats(names) {
      var all = {};
      LIN_ALL.forEach(function (f) {
        all[f] = true;
      });
      if (!names || !names.length) return all;
      var wanted = {};
      for (var i = 0; i < names.length; i++) {
        var key = String(names[i]).toLowerCase().replace(/[^a-z0-9]/g, "");
        if (Object.prototype.hasOwnProperty.call(LIN_ALIASES, key)) {
          if (LIN_ALIASES[key] === null) return all;
          wanted[LIN_ALIASES[key]] = true;
        }
      }
      return wanted;
    }
    function boxFilter1D(values, radius, f32) {
      // moving average with edge replication; f32: cumulative sum in float32 (numpy on float32 input)
      if (radius < 1) return values;
      var n = values.length, width = 2 * radius + 1, csum = new Float64Array(n + 2 * radius + 1), acc = 0;
      for (var i = 0; i < n + 2 * radius; i++) {
        var v = values[i < radius ? 0 : i >= n + radius ? n - 1 : i - radius];
        acc = f32 ? fr(acc + v) : acc + v;
        csum[i + 1] = acc;
      }
      var out = new Float64Array(n);
      for (var j = 0; j < n; j++) out[j] = (csum[j + width] - csum[j]) / width;
      return out;
    }
    function movingExtreme(values, radius, isMax) {
      var n = values.length, out = new Float64Array(n);
      for (var i = 0; i < n; i++) {
        var e = isMax ? -Infinity : Infinity;
        for (var k = i - radius; k <= i + radius; k++) {
          var v = values[k < 0 ? 0 : k >= n ? n - 1 : k];
          if (isMax ? v > e : v < e) e = v;
        }
        out[i] = e;
      }
      return out;
    }
    // np.percentile(..., method="linear") of a sorted copy
    function percentileLinear(sorted, q) {
      var n = sorted.length, vi = (n - 1) * (q / 100), lo = Math.floor(vi), hi = Math.min(lo + 1, n - 1), g = vi - lo;
      var a = sorted[lo], b = sorted[hi], diff = b - a;
      return g >= 0.5 ? b - diff * (1 - g) : a + diff * g;
    }
    // Mean intensity profiles of `count` horizontal bands (float32 like numpy)
    function scanlineProfiles(gray, count, band) {
      var h = gray.height, w = gray.width, d = gray.data;
      var top = Math.trunc(h * 0.08), bottom = Math.max(Math.trunc(h * 0.92), Math.trunc(h * 0.08) + 1);
      var step = (bottom - 1 - top) / (count - 1), centres = [];
      for (var i = 0; i < count; i++) {
        var c = Math.trunc(i === count - 1 ? bottom - 1 : i * step + top);
        if (centres.indexOf(c) < 0) centres.push(c);
      }
      centres.sort(function (a, b) {
        return a - b;
      });
      return centres.map(function (c) {
        var y0 = Math.max(c - (band >> 1), 0), y1 = Math.min(c + (band >> 1) + 1, h), row = new Float32Array(w);
        for (var x = 0; x < w; x++) {
          var s = 0;
          for (var y = y0; y < y1; y++) s += d[y * w + x];
          row[x] = s / (y1 - y0);
        }
        return row;
      });
    }
    // Sub-pixel bar/space boundaries of one scanline: {edges, firstDark} or null
    function lineEdges(line, minContrast) {
      var n = line.length, i;
      if (n < 20) return null;
      var sorted = Float64Array.from(line).sort();
      var lo = percentileLinear(sorted, 3), hi = percentileLinear(sorted, 97);
      if (hi - lo < (minContrast || 40)) return null;
      var radius = Math.max(Math.trunc(n / 24), 6);
      var emax = boxFilter1D(movingExtreme(line, radius, true), radius >> 1, false);
      var emin = boxFilter1D(movingExtreme(line, radius, false), radius >> 1, false);
      var thr = new Float64Array(n), dark = new Uint8Array(n), weakLevel = (hi - lo) * 0.35, mid = (hi + lo) / 2.0;
      for (i = 0; i < n; i++) {
        thr[i] = emax[i] - emin[i] < weakLevel ? mid : (emax[i] + emin[i]) / 2.0;
        dark[i] = line[i] < thr[i] ? 1 : 0;
      }
      var change = [];
      for (i = 0; i < n - 1; i++) if (dark[i + 1] !== dark[i]) change.push(i);
      if (change.length < 6) return null;
      var edges = change.map(function (c) {
        var a = line[c] - thr[c], b = line[c + 1] - thr[c + 1], denom = a === b ? 1.0 : a - b;
        return c + clamp(a / denom, 0.0, 1.0) + 0.5;
      });
      return { edges: edges, firstDark: !!dark[0] };
    }
    function runsFromEdges(edges, firstDark, length) {
      var out = firstDark ? [0.0] : [], prev = 0.0;
      for (var i = 0; i < edges.length; i++) {
        out.push(edges[i] - prev);
        prev = edges[i];
      }
      out.push(length - prev);
      return out;
    }
    function linMatch(widths, patterns, totalModules) {
      var total = 0, i, k;
      for (i = 0; i < widths.length; i++) total += widths[i];
      if (total <= 0) return -1;
      var module = total / totalModules, best = -1, bestErr = 1e9, secondErr = 1e9;
      for (i = 0; i < patterns.length; i++) {
        var p = patterns[i], err = 0, m = Math.min(widths.length, p.length);
        for (k = 0; k < m; k++) err += Math.abs(widths[k] / module - p[k]);
        err /= p.length;
        if (err < bestErr) {
          secondErr = bestErr;
          bestErr = err;
          best = i;
        } else if (err < secondErr) secondErr = err;
      }
      if (bestErr > MAX_PATTERN_ERROR || secondErr - bestErr < MIN_PATTERN_MARGIN) return -1;
      return best;
    }
    function quietEnough(space, module, required, atBorder) {
      return atBorder || space >= module * required;
    }
    function okRatio(a, b, tolerance) {
      return Math.abs(a - b) <= (tolerance === undefined ? 0.35 : tolerance) * Math.max(a, b);
    }
    function quietBefore(runs, i, count, totalModules, required) {
      var total = 0;
      for (var k = i; k < i + count; k++) total += runs[k];
      var module = total / totalModules;
      if (i === 1 && runs[0] >= 2 * module) return true;
      return runs[i - 1] >= module * required * 0.7;
    }
    function elSign(k, firstIsBar) {
      return (k % 2 === 0) === firstIsBar ? 1.0 : -1.0;
    }
    // Least squares widths = module * pattern + sign * bias -> [module, bias, error]
    function linFit(widths, pattern, firstIsBar) {
      var spp = 0, sps = 0, sss = 0, swp = 0, sws = 0, k;
      for (k = 0; k < pattern.length; k++) {
        var p = pattern[k], s = elSign(k, firstIsBar), w = widths[k];
        spp += p * p;
        sps += p * s;
        sss += s * s;
        swp += w * p;
        sws += w * s;
      }
      var det = spp * sss - sps * sps;
      if (det <= 0) return [0.0, 0.0, 1e9];
      var module = (swp * sss - sws * sps) / det, bias = (spp * sws - sps * swp) / det;
      if (module <= 0) return [0.0, 0.0, 1e9];
      var limit = 0.45 * module;
      bias = Math.max(-limit, Math.min(limit, bias));
      var err = 0;
      for (k = 0; k < pattern.length; k++) err += Math.abs(widths[k] - module * pattern[k] - elSign(k, firstIsBar) * bias);
      return [module, bias, err / (pattern.length * module)];
    }
    function linMatchFit(widths, patterns, firstIsBar) {
      var best = -1, bestErr = 1e9, secondErr = 1e9, fit = [0.0, 0.0];
      for (var i = 0; i < patterns.length; i++) {
        var r = linFit(widths, patterns[i], firstIsBar);
        if (r[2] < bestErr) {
          secondErr = bestErr;
          bestErr = r[2];
          best = i;
          fit = [r[0], r[1]];
        } else if (r[2] < secondErr) secondErr = r[2];
      }
      if (bestErr > MAX_PATTERN_ERROR || secondErr - bestErr < MIN_PATTERN_MARGIN) return [-1, 0.0, 0.0];
      return [best, fit[0], fit[1]];
    }
    function debias(widths, bias, firstIsBar) {
      return widths.map(function (w, k) {
        return Math.max(w - elSign(k, firstIsBar) * bias, 0.05);
      });
    }
    function updateBias(bias, widths, pattern, firstIsBar) {
      var r = linFit(widths, pattern, firstIsBar);
      return r[2] > MAX_PATTERN_ERROR ? bias : 0.7 * bias + 0.3 * r[1];
    }
    function sumOf(a) {
      var s = 0;
      for (var i = 0; i < a.length; i++) s += a[i];
      return s;
    }
    function decodeCode128(runs) {
      var n = runs.length, starts = C128_PATTERNS.slice(103, 106);
      for (var i = 1; i < n - 6 * 3 - 7; i += 2) {
        if (!quietBefore(runs, i, 6, 11, 5)) continue;
        var m = linMatchFit(runs.slice(i, i + 6), starts, true);
        if (m[0] < 0) continue;
        if (!quietEnough(runs[i - 1], m[1], 5, i === 1 && runs[0] >= 2 * m[1])) continue;
        var result = code128From(runs, i, 103 + m[0], m[1], m[2]);
        if (result) return result;
      }
      return null;
    }
    function code128From(runs, i, startValue, module, bias) {
      var values = [startValue], pos = i + 6, n = runs.length, data = C128_PATTERNS.slice(0, 106);
      while (pos + 7 <= n) {
        var stopWidths = debias(runs.slice(pos, pos + 7), bias, true), stopModule = sumOf(stopWidths) / 13.0;
        if (okRatio(stopModule, module) && linMatch(stopWidths, [C128_STOP], 13) === 0) {
          var trailing = pos + 7 < n ? runs[pos + 7] : module * 10;
          if (quietEnough(trailing, module, 5, pos + 8 >= n) && values.length >= 3) return code128Text(values);
        }
        var raw = runs.slice(pos, pos + 6), charWidths = debias(raw, bias, true), charModule = sumOf(charWidths) / 11.0;
        if (!okRatio(charModule, module)) return null;
        var value = linMatch(charWidths, data, 11);
        if (value < 0 || value >= 103) return null;
        values.push(value);
        bias = updateBias(bias, raw, C128_PATTERNS[value], true);
        module = 0.8 * module + 0.2 * charModule;
        pos += 6;
      }
      return null;
    }
    function code128Text(values) {
      var checksum = values[0], k;
      for (k = 1; k < values.length - 1; k++) checksum += k * values[k];
      if (checksum % 103 !== values[values.length - 1]) return null;
      var codeSet = { 103: "A", 104: "B", 105: "C" }[values[0]], out = [], shift = false, fnc4Next = false, fnc4Latched = false;
      var data = values.slice(1, -1);
      for (var index = 0; index < data.length; index++) {
        var value = data[index], current = codeSet;
        if (shift) {
          current = codeSet === "A" ? "B" : "A";
          shift = false;
        }
        if (current === "C") {
          if (value < 100) out.push((value < 10 ? "0" : "") + value);
          else if (value === C128_CODE_B) codeSet = "B";
          else if (value === C128_CODE_A) codeSet = "A";
          else if (value === C128_FNC1 && index > 0) out.push("\x1d");
          continue;
        }
        if (value < 96) {
          var code = current === "A" ? (value < 64 ? value + 32 : value - 64) : value + 32;
          if (fnc4Next || fnc4Latched) {
            code += 128;
            fnc4Next = false;
          }
          out.push(String.fromCharCode(code));
          continue;
        }
        if (value === C128_FNC1) {
          if (index > 0) out.push("\x1d");
        } else if (value === C128_SHIFT) shift = true;
        else if (value === C128_CODE_C) codeSet = "C";
        else if ((current === "A" && value === C128_CODE_A) || (current === "B" && value === C128_CODE_B)) {
          if (fnc4Next) {
            fnc4Latched = !fnc4Latched;
            fnc4Next = false;
          } else fnc4Next = true;
        } else if (value === C128_CODE_A) codeSet = "A";
        else if (value === C128_CODE_B) codeSet = "B";
      }
      var text = out.join("");
      return text ? [text, LIN.CODE128] : null;
    }
    // [char, narrow, wide, bias] for 9 elements (bar first), or null
    function code39Char(widths, bias) {
      widths = debias(widths, bias || 0.0, true);
      var ordered = widths.slice().sort(function (a, b) {
        return a - b;
      });
      var narrowMax = ordered[5], wideMin = ordered[6];
      if (wideMin < narrowMax * 1.5) return null;
      var narrow = (ordered[0] + ordered[1] + ordered[2] + ordered[3] + ordered[4] + ordered[5]) / 6.0, wide = (ordered[6] + ordered[7] + ordered[8]) / 3.0;
      if (ordered[0] < narrow * 0.4 || ordered[8] > wide * 1.6) return null;
      var threshold = (narrowMax + wideMin) / 2.0, code = 0, nb = [], ns = [];
      for (var k = 0; k < widths.length; k++) {
        var isWide = widths[k] > threshold;
        code = (code << 1) | (isWide ? 1 : 0);
        if (!isWide) (k % 2 === 0 ? nb : ns).push(widths[k]);
      }
      var ch = C39_BY_CODE[code];
      if (ch === undefined) return null;
      var spread = nb.length && ns.length ? (sumOf(nb) / nb.length - sumOf(ns) / ns.length) / 2.0 : 0.0;
      return [ch, narrow, wide, (bias || 0.0) + spread];
    }
    function decodeCode39(runs, checkDigit, extended) {
      var n = runs.length;
      for (var i = 1; i < n - 9 * 3; i += 2) {
        if (!quietBefore(runs, i, 9, 15, 7)) continue;
        var first = code39Char(runs.slice(i, i + 9));
        if (!first || first[0] !== "*") continue;
        if (!quietEnough(runs[i - 1], first[1], 7, i === 1 && runs[0] >= 2 * first[1])) continue;
        var result = code39From(runs, i + 9, first[1], first[2], first[3], checkDigit, extended);
        if (result) return result;
      }
      return null;
    }
    function code39From(runs, pos, narrow, wide, bias, checkDigit, extended) {
      var chars = [], n = runs.length;
      while (pos + 10 <= n) {
        var gap = runs[pos] + bias;
        if (gap < narrow * 0.4 || gap > narrow * 3.2) return null;
        var decoded = code39Char(runs.slice(pos + 1, pos + 10), bias);
        if (!decoded) return null;
        if (!(okRatio(decoded[1], narrow, 0.45) && okRatio(decoded[2], wide))) return null;
        bias = 0.7 * bias + 0.3 * decoded[3];
        pos += 10;
        if (decoded[0] === "*") {
          var trailing = pos < n ? runs[pos] : narrow * 10;
          if (!quietEnough(trailing, narrow, 7, pos + 1 >= n)) return null;
          if (!chars.length) return null;
          var text = chars.join("");
          if (checkDigit) {
            if (text.length < 2) return null;
            var total = 0;
            for (var c = 0; c < text.length - 1; c++) total += C39_ALPHABET.indexOf(text[c]);
            if (C39_ALPHABET[total % 43] !== text[text.length - 1]) return null;
            text = text.slice(0, -1);
          }
          if (extended) {
            var full = code39FullAscii(text);
            if (full !== null) text = full;
            else if (extended === true) return null;
          }
          return [text, LIN.CODE39];
        }
        chars.push(decoded[0]);
      }
      return null;
    }
    function code39FullAscii(text) {
      var out = [], i = 0;
      function inRange(c, a, b) {
        return c >= a && c <= b;
      }
      while (i < text.length) {
        var ch = text[i];
        if ("$%/+".indexOf(ch) >= 0) {
          if (i + 1 >= text.length) return null;
          var nx = text[i + 1], o = nx.charCodeAt(0);
          i += 2;
          if (ch === "+" && inRange(nx, "A", "Z")) out.push(String.fromCharCode(o + 32));
          else if (ch === "$" && inRange(nx, "A", "Z")) out.push(String.fromCharCode(o - 64));
          else if (ch === "%" && inRange(nx, "A", "E")) out.push(String.fromCharCode(o - 38));
          else if (ch === "%" && inRange(nx, "F", "J")) out.push(String.fromCharCode(o - 11));
          else if (ch === "%" && inRange(nx, "K", "O")) out.push(String.fromCharCode(o + 16));
          else if (ch === "%" && inRange(nx, "P", "T")) out.push(String.fromCharCode(o + 43));
          else if (ch === "%" && nx === "U") out.push("\x00");
          else if (ch === "%" && nx === "V") out.push("@");
          else if (ch === "%" && nx === "W") out.push("`");
          else if (ch === "%" && "XYZ".indexOf(nx) >= 0) out.push(String.fromCharCode(127));
          else if (ch === "/" && inRange(nx, "A", "O")) out.push(String.fromCharCode(o - 32));
          else if (ch === "/" && nx === "Z") out.push(":");
          else return null;
        } else {
          out.push(ch);
          i++;
        }
      }
      return out.join("");
    }
    function decodeItf(runs, minLength, checkDigit) {
      var n = runs.length;
      for (var i = 1; i < n - 4 - 10 - 3; i += 2) {
        if (!quietBefore(runs, i, 4, 4, 8)) continue;
        var f = linFit(runs.slice(i, i + 4), [1, 1, 1, 1], true);
        if (f[2] > MAX_PATTERN_ERROR || f[0] <= 0) continue;
        if (!quietEnough(runs[i - 1], f[0], 8, i === 1 && runs[0] >= 2 * f[0])) continue;
        var result = itfFrom(runs, i + 4, f[0], f[1], minLength, checkDigit);
        if (result) return result;
      }
      return null;
    }
    function itfFrom(runs, pos, narrow, bias, minLength, checkDigit) {
      var digits = [], n = runs.length;
      while (pos + 3 <= n) {
        var stop = debias(runs.slice(pos, pos + 3), bias, true);
        if (digits.length >= minLength && stop[0] > narrow * 1.7 && stop[1] < narrow * 1.6 && stop[2] < narrow * 1.6) {
          var trailing = pos + 3 < n ? runs[pos + 3] : narrow * 10;
          if (quietEnough(trailing, narrow, 8, pos + 4 >= n)) {
            var text = digits.join("");
            if (checkDigit && !mod10Ok(text)) return null;
            return [text, LIN.ITF];
          }
        }
        if (pos + 10 > n) break;
        var raw = runs.slice(pos, pos + 10), block = debias(raw, bias, true), pairModule = sumOf(block) / 14.0;
        if (!okRatio(pairModule, narrow, 0.45)) return null;
        var first = linMatch([block[0], block[2], block[4], block[6], block[8]], ITF_PATTERNS, 7);
        var second = linMatch([block[1], block[3], block[5], block[7], block[9]], ITF_PATTERNS, 7);
        if (first < 0 || second < 0) return null;
        digits.push(first, second);
        var pattern = [];
        for (var k = 0; k < 5; k++) pattern.push(ITF_PATTERNS[first][k], ITF_PATTERNS[second][k]);
        bias = updateBias(bias, raw, pattern, true);
        narrow = 0.8 * narrow + 0.2 * pairModule;
        pos += 10;
      }
      return null;
    }
    function mod10Ok(text) {
      if (text.length < 2 || !/^[0-9]+$/.test(text)) return false;
      var total = 0;
      for (var off = 0; off < text.length - 1; off++) total += Number(text[text.length - 2 - off]) * (off % 2 === 0 ? 3 : 1);
      return (10 - (total % 10)) % 10 === Number(text[text.length - 1]);
    }
    function decodeEan(runs, wanted) {
      var n = runs.length;
      for (var i = 1; i < n - 3; i += 2) {
        if (!quietBefore(runs, i, 3, 3, 5)) continue;
        var f = linFit(runs.slice(i, i + 3), [1, 1, 1], true);
        if (f[2] > MAX_PATTERN_ERROR || f[0] <= 0) continue;
        if (!quietEnough(runs[i - 1], f[0], 5, i === 1 && runs[0] >= 2 * f[0])) continue;
        var halves = [6, 4];
        for (var h = 0; h < 2; h++) {
          if (halves[h] === 6 && !(wanted[LIN.EAN13] || wanted[LIN.UPCA])) continue;
          if (halves[h] === 4 && !wanted[LIN.EAN8]) continue;
          var result = eanFrom(runs, i + 3, f[0], f[1], halves[h], wanted);
          if (result) return result;
        }
      }
      return null;
    }
    function eanSide(runs, pos, module, bias, count, left) {
      var firstIsBar = !left, candidates = left ? EAN_LG : EAN_L, digits = [], parities = [];
      for (var c = 0; c < count; c++) {
        var raw = runs.slice(pos, pos + 4);
        if (raw.length < 4) return null;
        var widths = debias(raw, bias, firstIsBar), digitModule = sumOf(widths) / 7.0;
        if (!okRatio(digitModule, module, 0.3)) return null;
        var index = linMatch(widths, candidates, 7);
        if (index < 0) return null;
        digits.push(index % 10);
        parities.push(Math.floor(index / 10));
        bias = updateBias(bias, raw, candidates[index], firstIsBar);
        module = 0.8 * module + 0.2 * digitModule;
        pos += 4;
      }
      return [digits, parities, pos, module, bias];
    }
    function eanFrom(runs, pos, module, bias, half, wanted) {
      var n = runs.length;
      if (pos + half * 4 * 2 + 5 + 3 > n) return null;
      var left = eanSide(runs, pos, module, bias, half, true);
      if (!left) return null;
      pos = left[2];
      module = left[3];
      bias = left[4];
      if (linFit(runs.slice(pos, pos + 5), [1, 1, 1, 1, 1], false)[2] > MAX_PATTERN_ERROR) return null;
      var right = eanSide(runs, pos + 5, module, bias, half, false);
      if (!right) return null;
      pos = right[2];
      module = right[3];
      var endGuard = runs.slice(pos, pos + 3);
      if (endGuard.length < 3 || linFit(endGuard, [1, 1, 1], true)[2] > MAX_PATTERN_ERROR) return null;
      var trailing = pos + 3 < n ? runs[pos + 3] : module * 10;
      if (!quietEnough(trailing, module, 5, pos + 4 >= n)) return null;
      var text;
      if (half === 6) {
        var first = EAN_FIRST_DIGIT[left[1].join("")];
        if (first === undefined) return null;
        text = String(first) + left[0].join("") + right[0].join("");
        if (!mod10Ok(text)) return null;
        if (text[0] === "0" && wanted[LIN.UPCA] && (!wanted[LIN.EAN13] || !UPCA_AS_EAN13)) return [text.slice(1), LIN.UPCA];
        if (wanted[LIN.EAN13]) return [text, LIN.EAN13];
        return null;
      }
      if (left[1].some(Boolean)) return null;
      text = left[0].join("") + right[0].join("");
      if (!mod10Ok(text)) return null;
      return [text, LIN.EAN8];
    }
    function decodeRuns(runs, wanted, options) {
      var found;
      if (wanted[LIN.CODE128] && (found = decodeCode128(runs))) return found;
      if ((wanted[LIN.EAN13] || wanted[LIN.EAN8] || wanted[LIN.UPCA]) && (found = decodeEan(runs, wanted))) return found;
      if (wanted[LIN.CODE39] && (found = decodeCode39(runs, options.code39Checksum || false, options.code39Extended === undefined ? "auto" : options.code39Extended))) return found;
      if (wanted[LIN.ITF] && (found = decodeItf(runs, options.itfMinLength === undefined ? 6 : options.itfMinLength, options.itfChecksum || false))) return found;
      return null;
    }
    // 1-D unsharp mask; profile is float32, the result float64 (as numpy)
    function sharpenProfile(profile, amount, radius) {
      var smooth = boxFilter1D(boxFilter1D(profile, radius, true), radius, false), out = new Float64Array(profile.length);
      for (var i = 0; i < profile.length; i++) out[i] = profile[i] + amount * (profile[i] - smooth[i]);
      return out;
    }
    function decodeLine(profile, width, wanted, options) {
      var e = lineEdges(profile);
      if (!e) return null;
      var runs = runsFromEdges(e.edges, e.firstDark, width), found = decodeRuns(runs, wanted, options);
      if (!found) {
        // right-to-left (upside-down code), keeping "leading space first"
        var reverse = runs.slice().reverse();
        if (runs.length % 2 === 0) reverse.unshift(0.0);
        found = decodeRuns(reverse, wanted, options);
      }
      return found;
    }
    function linearScan(gray, wanted, options, minAgree, lines) {
      var votes = [], total = 0, profiles = scanlineProfiles(gray, lines, 3);
      for (var p = 0; p < profiles.length; p++) {
        var profile = profiles[p], found = decodeLine(profile, gray.width, wanted, options);
        if (!found) found = decodeLine(sharpenProfile(profile, 1.5, 1), gray.width, wanted, options);
        if (!found) found = decodeLine(sharpenProfile(profile, 3.0, 2), gray.width, wanted, options);
        if (!found) continue;
        var entry = null;
        for (var v = 0; v < votes.length; v++) if (votes[v].text === found[0] && votes[v].format === found[1]) entry = votes[v];
        if (!entry) votes.push((entry = { text: found[0], format: found[1], count: 0 }));
        entry.count++;
        total++;
        if (entry.count >= minAgree && entry.count * 2 > total) return entry;
      }
      var best = null;
      votes.forEach(function (e) {
        if (!best || e.count > best.count) best = e;
      });
      return best && best.count >= minAgree ? best : null;
    }
    function rot90(img) {
      // np.rot90 (counter-clockwise): out[i][j] = img[j][w - 1 - i]
      var w = img.width, h = img.height, out = new Uint8Array(w * h);
      for (var i = 0; i < w; i++) for (var j = 0; j < h; j++) out[i * h + j] = img.data[j * w + (w - 1 - i)];
      return makeImage(h, w, out);
    }
    // linear.decode: {text, format, votes, rotated} or null
    function decodeLinear(gray, formats, options, minAgree, lines) {
      var wanted = builtinFormats(formats);
      if (!Object.keys(wanted).length) return null;
      var w = gray.width, h = gray.height, orientations = w >= h ? [false, true] : [true, false];
      if (Math.max(w, h) > 2 * Math.min(w, h)) orientations = orientations.slice(0, 1);
      for (var k = 0; k < orientations.length; k++) {
        var found = linearScan(orientations[k] ? rot90(gray) : gray, wanted, options || {}, minAgree || 2, lines || 12);
        if (found) return { text: found.text, format: found.format, votes: found.count, rotated: orientations[k] };
      }
      return null;
    }

    // ------------------------------------------------------------------------
    // Zones (barcode / QR via zxing-wasm; OCR / ICR via a registered hook)
    // ------------------------------------------------------------------------
    var zoneReaders = {};
    var zxing = { lib: null, loading: null, options: {} };

    function loadScript(url) {
      return new Promise(function (resolve, reject) {
        if (typeof importScripts === "function" && typeof document === "undefined") {
          try {
            importScripts(url);
            resolve();
          } catch (e) {
            reject(e);
          }
          return;
        }
        if (typeof document === "undefined") return reject(new Error("Cannot load scripts in this environment: " + url));
        var s = document.createElement("script");
        s.src = url;
        s.async = true;
        s.crossOrigin = "anonymous";
        s.onload = function () {
          resolve();
        };
        s.onerror = function () {
          reject(new Error("Failed to load " + url));
        };
        document.head.appendChild(s);
      });
    }

    // OMR.enableBarcodes({ module?, scriptUrl?, wasmUrl?, tryHarder? })
    function enableBarcodes(opts) {
      opts = opts || {};
      zxing.options = opts;
      if (opts.module) {
        zxing.loading = Promise.resolve(opts.module).then(function (mod) {
          return prepareZxing(mod, opts);
        });
        return zxing.loading;
      }
      zxing.loading = (root && root.ZXingWASM ? Promise.resolve() : loadScript(opts.scriptUrl || DEFAULT_LIBS.zxingScriptUrl)).then(function () {
        if (!root.ZXingWASM) throw new Error("zxing-wasm did not load (ZXingWASM global missing)");
        return prepareZxing(root.ZXingWASM, opts);
      });
      return zxing.loading;
    }
    function prepareZxing(mod, opts) {
      var overrides = {};
      if (opts.wasmBinary) overrides.wasmBinary = opts.wasmBinary;
      else {
        var wasmUrl = opts.wasmUrl || DEFAULT_LIBS.zxingWasmUrl;
        overrides.locateFile = function (path, prefix) {
          return path.slice(-5) === ".wasm" ? wasmUrl : prefix + path;
        };
      }
      var ready;
      if (mod.prepareZXingModule) ready = mod.prepareZXingModule({ overrides: overrides, fireImmediately: true });
      else if (mod.getZXingModule) ready = mod.getZXingModule(overrides);
      else ready = Promise.resolve();
      return Promise.resolve(ready).then(function () {
        zxing.lib = mod;
        return true;
      });
    }
    function barcodesAvailable() {
      return !!zxing.lib;
    }
    var ZX_FORMAT_ALIASES = { RMQRCode: "rMQRCode", rmqrcode: "rMQRCode", DataBarExpanded: "DataBarExpanded", UPCA: "UPC-A", UPCE: "UPC-E", EAN8: "EAN-8", EAN13: "EAN-13" };
    function zxFormats(zone) {
      var names = zone.options.formats;
      if (!names || !names.length) return zone.type === "qrcode" ? ["QRCode", "MicroQRCode", "rMQRCode"] : [];
      return names.map(function (n) {
        return ZX_FORMAT_ALIASES[n] || n;
      });
    }
    function grayToImageData(img) {
      var n = img.width * img.height, rgba = new Uint8ClampedArray(n * 4);
      for (var i = 0, j = 0; i < n; i++, j += 4) {
        var v = img.data[i];
        rgba[j] = rgba[j + 1] = rgba[j + 2] = v;
        rgba[j + 3] = 255;
      }
      if (typeof ImageData !== "undefined") {
        try {
          return new ImageData(rgba, img.width, img.height);
        } catch (e) {
          /* fall through */
        }
      }
      return { data: rgba, width: img.width, height: img.height, colorSpace: "srgb" };
    }
    function formatLabel(fmt) {
      var mod = zxing.lib;
      if (mod && mod.formatToLabel) {
        try {
          return mod.formatToLabel(fmt);
        } catch (e) {
          /* ignore */
        }
      }
      return fmt;
    }
    // zxing-wasm: progressively heavier preprocessing until something decodes.
    // Resolves to null when zxing-wasm is not loaded, else [symbols, details].
    function readZxing(zone, crop) {
      if (!zxing.lib) return Promise.resolve(null);
      var formats = zxFormats(zone);
      var options = { tryHarder: zxing.options.tryHarder !== false, maxNumberOfSymbols: 8 };
      if (formats.length) options.formats = formats;
      var attempts = [
        function () {
          return crop;
        },
        function () {
          return Math.max(crop.width, crop.height) < 1200 ? resizeLinear(crop, crop.width * 2, crop.height * 2) : null;
        },
        function () {
          return thresholdBinary(crop, otsuValue(crop), false);
        },
        function () {
          return gaussianBlur(crop, 3, 3, 0);
        },
        function () {
          // blurred 1-D codes: average along the bars, then stretch across them
          return resizeLinear(sepFilter(crop, [1], new Array(15).fill(1 / 15)), crop.width * 3, crop.height);
        },
      ];
      var i = 0;
      function next() {
        if (i >= attempts.length) return Promise.resolve([[], {}]);
        var img = attempts[i++]();
        if (!img || !img.width || !img.height) return next();
        return Promise.resolve(zxing.lib.readBarcodes(grayToImageData(img), options)).then(function (symbols) {
          symbols = (symbols || []).filter(function (s) {
            return s.isValid !== false || s.text;
          });
          if (!symbols.length) return next();
          return [
            symbols.map(function (s) {
              return { text: s.text, format: formatLabel(s.format), valid: !!s.isValid };
            }),
            { orientation: symbols[0].orientation || 0 },
          ];
        });
      }
      return next();
    }
    // The built-in scanline decoder: null when not applicable (QR zones, formats it lacks)
    function readBuiltin(zone, crop) {
      if (zone.type === "qrcode") return null;
      var formats = zone.options.formats;
      if (!Object.keys(builtinFormats(formats)).length) return null;
      var found = decodeLinear(crop, formats, zone.options);
      if (!found) return [[], {}];
      return [[{ text: found.text, format: found.format, valid: true }], { scanline_votes: found.votes, rotated: found.rotated }];
    }
    var BARCODE_ENGINES = ["zxing", "builtin", "opencv", "pyzbar"];
    // barcode.py engine_order: zone options.engines, else barcode_params.engines
    function engineOrder(zone, params) {
      params = params || {};
      var order = zone.options.engines || params.engines || BARCODE_ENGINES;
      var pyzbarOn = zone.options.pyzbar !== undefined ? zone.options.pyzbar : params.pyzbar || false;
      return order.filter(function (name) {
        return name !== "pyzbar" || pyzbarOn;
      });
    }
    // Engines tried in order until one reads ("opencv" and "pyzbar" exist only in Python)
    function readBarcodeZone(zone, aligned, params) {
      var crop = cropImage(aligned, zone.origin[0] - 10, zone.origin[1] - 10, zone.dimensions[0] + 20, zone.dimensions[1] + 20);
      var order = engineOrder(zone, params), tried = [], k = 0;
      function finish(symbols, details, engine) {
        if (!tried.length) return zoneResult(zone, "", 0.0, ["engine_unavailable"]);
        if (!symbols.length) {
          var nf = zoneResult(zone, "", 0.0, ["not_found"]);
          nf.details = { engines: tried };
          return nf;
        }
        var flags = [];
        if (symbols.length > 1) flags.push("multiple_symbols");
        if (engine !== "zxing") flags.push("decoded_by_fallback");
        var s0 = symbols[0], r = zoneResult(zone, s0.text, s0.valid ? 1.0 : 0.0, flags);
        r.format = s0.format;
        r.details = Object.assign({ symbols: symbols, engines: tried }, details);
        r.engine = engine;
        return r;
      }
      function next(symbols, details) {
        if (k >= order.length) return Promise.resolve(finish(symbols, details, null));
        var name = order[k++];
        var outcome = name === "zxing" ? readZxing(zone, crop) : name === "builtin" ? readBuiltin(zone, crop) : null;
        return Promise.resolve(outcome).then(function (res) {
          if (!res) return next(symbols, details);
          tried.push(name);
          if (res[0].length) return finish(res[0], res[1], name);
          return next(res[0], res[1]);
        });
      }
      return next([], {});
    }
    function zoneResult(zone, value, confidence, flags) {
      return { name: zone.name, type: zone.type, value: value, confidence: confidence, flags: flags || [], needs_review: false, box: [], format: null, details: {}, engine: null };
    }
    // Placeholder for a lazy zone that was not needed (read later only if a check asks)
    function skippedZone(zone) {
      var r = zoneResult(zone, zone.emptyValue, 0.0, ["not_read"]);
      r.box = [zone.origin[0], zone.origin[1], zone.dimensions[0], zone.dimensions[1]];
      return r;
    }
    function finalizeZone(result, zone, extraReviewFlags) {
      var o = zone.options || {};
      if (o.pattern && result.value) {
        var re = pyRegex(o.pattern, true);
        if (re && !re.test(result.value)) result.flags.push("pattern_mismatch");
      }
      var minConf = o.minConfidence !== undefined ? o.minConfidence : 0.6;
      if (result.value && result.confidence < minConf) result.flags.push("low_confidence");
      var set = {};
      result.flags.forEach(function (f) {
        set[f] = 1;
      });
      result.flags = Object.keys(set).sort();
      result.needs_review = result.flags.some(function (f) {
        return ZONE_REVIEW_FLAGS.indexOf(f) >= 0 || (extraReviewFlags || []).indexOf(f) >= 0;
      });
      if (!result.value) result.value = zone.emptyValue;
      return result;
    }
    function readZone(zone, aligned, readers, barcodeParams) {
      var box = [zone.origin[0], zone.origin[1], zone.dimensions[0], zone.dimensions[1]];
      var p;
      try {
        if (zone.type === "barcode" || zone.type === "qrcode") p = readBarcodeZone(zone, aligned, barcodeParams);
        else if (zone.type === "ocr" || zone.type === "icr") {
          var hook = (readers && readers[zone.type]) || zoneReaders[zone.type];
          if (!hook) p = Promise.resolve(zoneResult(zone, "", 0, zone.type === "icr" ? ["no_icr_model", "engine_unavailable"] : ["engine_unavailable"]));
          else {
            var crop = cropImage(aligned, zone.origin[0] - 2, zone.origin[1] - 2, zone.dimensions[0] + 4, zone.dimensions[1] + 4);
            p = Promise.resolve(hook(crop, zone)).then(function (out) {
              out = out || {};
              var r = zoneResult(zone, out.value || "", out.confidence !== undefined ? out.confidence : 0, (out.flags || []).slice());
              if (out.details) r.details = out.details;
              return r;
            });
          }
        } else if (zone.type === "image") {
          // Image zones (photo, signature) keep the crop; see src/readers/image_zone.py.
          // The gray crop is on result.crop (not serialised); saving it is up to the page.
          var imageCrop = cropImage(aligned, zone.origin[0], zone.origin[1], zone.dimensions[0], zone.dimensions[1]);
          var ir = zoneResult(zone, "", 1, []);
          ir.details = { width: imageCrop.width, height: imageCrop.height };
          Object.defineProperty(ir, "crop", { value: imageCrop, enumerable: false });
          p = Promise.resolve(ir);
        } else throw new Error("Unknown zone type: " + zone.type);
      } catch (e) {
        p = Promise.reject(e);
      }
      return p
        .catch(function (error) {
          var r = zoneResult(zone, "", 0, ["read_error"]);
          r.details = { error: String((error && error.message) || error) };
          return r;
        })
        .then(function (r) {
          r.box = box;
          var extra = barcodeParams && barcodeParams.review_fallback_decodes ? ["decoded_by_fallback"] : [];
          return finalizeZone(r, zone, extra);
        });
    }

    // ------------------------------------------------------------------------
    // Optional ONNX bubble classifier (same sidecar format as src/ml/classifiers.py)
    // ------------------------------------------------------------------------
    function softmaxRows(logits, rows, cols, temperature) {
      var out = new Float32Array(rows * cols);
      for (var r = 0; r < rows; r++) {
        var mx = -Infinity, s = 0;
        for (var c = 0; c < cols; c++) mx = Math.max(mx, logits[r * cols + c] / (temperature || 1));
        for (var c2 = 0; c2 < cols; c2++) {
          var e = Math.exp(logits[r * cols + c2] / (temperature || 1) - mx);
          out[r * cols + c2] = e;
          s += e;
        }
        for (var c3 = 0; c3 < cols; c3++) out[r * cols + c3] /= s;
      }
      return out;
    }
    function loadBubbleModel(opts) {
      opts = opts || {};
      var ortReady = opts.ort
        ? Promise.resolve(opts.ort)
        : root && root.ort
        ? Promise.resolve(root.ort)
        : loadScript(opts.ortScriptUrl || DEFAULT_LIBS.ortScriptUrl).then(function () {
            if (!root.ort) throw new Error("onnxruntime-web did not load (ort global missing)");
            return root.ort;
          });
      var metaReady = opts.metadata ? Promise.resolve(opts.metadata) : fetch(opts.metadataUrl || String(opts.modelUrl).replace(/\.onnx(\?.*)?$/, ".json")).then(function (r) {
        if (!r.ok) throw new Error("Failed to load model metadata");
        return r.json();
      });
      return Promise.all([ortReady, metaReady]).then(function (res) {
        var ort = res[0], meta = res[1];
        if (ort.env && ort.env.wasm && !opts.ort) {
          ort.env.wasm.wasmPaths = opts.ortWasmPaths || DEFAULT_LIBS.ortWasmPaths;
          if (opts.numThreads) ort.env.wasm.numThreads = opts.numThreads;
        }
        return ort.InferenceSession.create(opts.model || opts.modelUrl, opts.sessionOptions || { executionProviders: ["wasm"] }).then(function (session) {
          var labels = meta.labels, size = meta.input_size || [32, 32], temperature = Number(meta.temperature || 1);
          var markedIndex = labels.indexOf("marked");
          if (markedIndex < 0) throw new Error("Bubble model labels must include 'marked'");
          var inputName = session.inputNames[0];
          return {
            labels: labels,
            inputSize: size,
            temperature: temperature,
            // Probability that each bubble is marked, in template traversal order
            predictMarked: function (img, template) {
              var crops = [];
              template.fieldBlocks.forEach(function (block) {
                var bw = block.bubbleDimensions[0], bh = block.bubbleDimensions[1];
                block.fields.forEach(function (f) {
                  f.bubbles.forEach(function (b) {
                    var x = b.x + block.shift + b.dx, y = b.y + block.shiftY + b.dy;
                    crops.push(cropImage(img, Math.max(x, 0), Math.max(y, 0), x + bw - Math.max(x, 0), y + bh - Math.max(y, 0)));
                  });
                });
              });
              var W = size[0], H = size[1], batch = new Float32Array(crops.length * W * H);
              crops.forEach(function (c, i) {
                if (!c.width || !c.height) return;
                var r = resizeArea(c, W, H);
                for (var k = 0; k < W * H; k++) batch[i * W * H + k] = r.data[k] / 255;
              });
              var feeds = {};
              feeds[inputName] = new ort.Tensor("float32", batch, [crops.length, 1, H, W]);
              return session.run(feeds).then(function (out) {
                var t = out[session.outputNames[0]], cols = labels.length;
                var probs = softmaxRows(t.data, crops.length, cols, temperature), marked = new Float32Array(crops.length);
                for (var i = 0; i < crops.length; i++) marked[i] = probs[i * cols + markedIndex];
                return marked;
              });
            },
          };
        });
      });
    }

    // ------------------------------------------------------------------------
    // Post-read rules: value validation ("validate") and cross-field checks
    // ("checks"). Port of src/rules (validation.py, checks.py, __init__.py).
    // ------------------------------------------------------------------------
    // A Python `re` pattern as a JS RegExp (full: re.fullmatch semantics); null if invalid
    function pyRegex(pattern, full) {
      var src = String(pattern)
        .replace(/\(\?P</g, "(?<")
        .replace(/\(\?P=(\w+)\)/g, "\\k<$1>")
        .replace(/\\A/g, "^")
        .replace(/\\Z/g, "$");
      try {
        return new RegExp(full ? "^(?:" + src + ")$" : src);
      } catch (e) {
        return null;
      }
    }
    function unicodeClass(cls, fallback) {
      try {
        return new RegExp("[" + cls + "]", "u");
      } catch (e) {
        return fallback;
      }
    }
    var DIGIT_RE = unicodeClass("\\p{Nd}", /[0-9]/), ALNUM_RE = unicodeClass("\\p{L}\\p{N}", /[0-9A-Za-z]/);
    function chars(text) {
      return Array.from(text);
    }
    function isBlank(text) {
      return !String(text).trim();
    }
    function pyNumber(text) {
      // float(text): Python syntax incl. underscores, inf and nan; NaN when invalid
      var s = String(text).trim();
      if (/^[+-]?(inf|infinity)$/i.test(s)) return s[0] === "-" ? -Infinity : Infinity;
      if (/^[+-]?nan$/i.test(s)) return NaN;
      if (!/^[+-]?((\d(_?\d)*)(\.(\d(_?\d)*)?)?|\.\d(_?\d)*)([eE][+-]?\d(_?\d)*)?$/.test(s)) return undefined;
      return Number(s.replace(/_/g, ""));
    }
    // Python repr() of a str (quotes and escapes as CPython prints them)
    function pyRepr(text) {
      var quote = text.indexOf("'") >= 0 && text.indexOf('"') < 0 ? '"' : "'", out = quote;
      for (var i = 0; i < text.length; i++) {
        var ch = text[i], code = text.charCodeAt(i);
        if (ch === "\\" || ch === quote) out += "\\" + ch;
        else if (ch === "\n") out += "\\n";
        else if (ch === "\r") out += "\\r";
        else if (ch === "\t") out += "\\t";
        else if (code < 0x20 || code === 0x7f) out += "\\x" + (code < 16 ? "0" : "") + code.toString(16);
        else out += ch;
      }
      return out + quote;
    }
    function pyStr(v) {
      return v === null || v === undefined ? "" : String(v);
    }

    // Per-group placeholders (template "groupOptions"; src/utils/parsing.py).
    // A group without an entry keeps the plain join.
    var GROUP_OPTION_DEFAULTS = { empty: " ", multi: "*", issue: "-" };
    function groupOptionsFor(template, name) {
      var options = (template.groupOptions || {})[name];
      if (!options) return null;
      return Object.assign({}, GROUP_OPTION_DEFAULTS, options);
    }
    function columnState(value, details, emptyValue) {
      var text = pyStr(value);
      var blank = !text.trim() || (emptyValue !== "" && text === emptyValue);
      if (!details) return blank ? "empty" : "ok";
      var flags = details.flags || [];
      if (blank) {
        if (!details.reviewed && details.needs_review && flags.some(function (f) { return f !== "empty"; })) return "issue";
        return "empty";
      }
      if (details.reviewed) return flags.indexOf("empty") >= 0 ? "empty" : "ok";
      if (flags.indexOf("multi_marked") >= 0) return "multi";
      if (flags.indexOf("empty") >= 0) return "empty";
      if (details.needs_review) return "issue";
      return "ok";
    }
    function joinGroup(columns, omr, options, fields, emptyValue) {
      var pieces = [], states = [];
      columns.forEach(function (column) {
        var value = has(omr, column) ? omr[column] : "";
        var state = columnState(value, fields ? fields[column] : null, emptyValue || "");
        states.push(state);
        if (!options || state === "ok") pieces.push(pyStr(value));
        else if (state === "empty") {
          if (options.empty !== null && options.empty !== undefined) pieces.push(String(options.empty));
        } else pieces.push(String(options[state] || ""));
      });
      return [pieces.join(""), states];
    }
    function describeGroups(omr, template, fields) {
      var groups = {};
      Object.keys(template.customLabels).forEach(function (name) {
        var options = groupOptionsFor(template, name);
        if (!options) return;
        var cols = template.customLabels[name];
        var joined = joinGroup(cols, omr, options, fields, template.emptyValue || "");
        groups[name] = {
          value: joined[0],
          columns: cols.map(function (c, i) {
            return { name: c, state: joined[1][i] };
          }),
          flagged: joined[1].some(function (st) {
            return st === "multi" || st === "issue";
          }),
        };
      });
      return groups;
    }
    function groupReviewItems(groups, review) {
      var listed = {};
      (review || []).forEach(function (item) {
        listed[item.name] = true;
      });
      var items = [];
      Object.keys(groups).forEach(function (name) {
        var group = groups[name];
        if (!group.flagged || listed[name]) return;
        var columns = group.columns.map(function (c) {
          return c.name;
        });
        if (columns.some(function (c) { return listed[c]; })) return;
        var flags = {};
        group.columns.forEach(function (c) {
          if (c.state === "multi") flags.multi_marked = true;
          if (c.state === "issue") flags.group_issue = true;
        });
        items.push({ kind: "custom_label", name: name, flags: Object.keys(flags).sort(), fields: columns });
      });
      return items;
    }

    var VALIDATION_FLAG = "validation_failed";
    var ON_FAIL_ACTIONS = ["review", "blank", "both", "flag"];
    function ValidationRule(name, spec) {
      this.name = name;
      this.length = spec.length === undefined ? null : spec.length;
      this.allowGaps = spec.allowGaps === undefined ? true : spec.allowGaps;
      this.allowEmptyEnds = spec.allowEmptyEnds === undefined ? true : spec.allowEmptyEnds;
      this.leadingZeros = spec.leadingZeros === undefined ? "keep" : spec.leadingZeros;
      this.required = !!spec.required;
      this.allowed = spec.allowed === undefined || spec.allowed === null ? null : spec.allowed.map(String);
      this.range = spec.range === undefined ? null : spec.range;
      this.onFail = spec.onFail === undefined ? "review" : spec.onFail;
      this.pattern = null;
      if (spec.pattern) {
        this.patternSource = spec.pattern;
        this.pattern = pyRegex(spec.pattern, true);
        if (!this.pattern) throw new Error("validate['" + name + "']: invalid pattern '" + spec.pattern + "'");
      }
      if (ON_FAIL_ACTIONS.indexOf(this.onFail) < 0) throw new Error("validate['" + name + "']: onFail must be one of ('review', 'blank', 'both', 'flag')");
      if (["keep", "forbid"].indexOf(this.leadingZeros) < 0) throw new Error("validate['" + name + "']: leadingZeros must be one of ('keep', 'forbid')");
      this.blanks = this.onFail === "blank" || this.onFail === "both";
      this.reviews = this.onFail === "review" || this.onFail === "both";
    }
    function bounds(spec) {
      if (Array.isArray(spec)) return [spec.length > 0 ? spec[0] : null, spec.length > 1 ? spec[1] : null];
      return [spec, spec];
    }
    function showBounds(spec) {
      if (Array.isArray(spec)) {
        var b = bounds(spec);
        return "[" + (b[0] === null ? "" : b[0]) + ", " + (b[1] === null ? "" : b[1]) + "]";
      }
      return String(spec);
    }
    // Reasons the value fails, or [] when it passes (ValidationRule.failures)
    ValidationRule.prototype.failures = function (value, columns, isEmpty) {
      value = pyStr(value);
      if (!columns) {
        columns = chars(value);
        isEmpty = columns.map(isBlank);
      } else if (!isEmpty) isEmpty = columns.map(isBlank);
      var filled = [];
      isEmpty.forEach(function (e, i) {
        if (!e) filled.push(i);
      });
      if (!filled.length) return this.required ? ["empty"] : [];
      var reasons = [], text = value, len = chars(text).length, b, i;
      if (this.length !== null) {
        b = bounds(this.length);
        if ((b[0] !== null && len < b[0]) || (b[1] !== null && len > b[1])) reasons.push("length " + len + " not in " + showBounds(this.length));
      }
      var first = filled[0], last = filled[filled.length - 1];
      if (!this.allowGaps) {
        var gaps = [];
        for (i = first; i <= last; i++) if (isEmpty[i]) gaps.push(i + 1);
        if (gaps.length) reasons.push("gap at position " + gaps.join(", "));
      }
      if (!this.allowEmptyEnds) {
        if (first > 0) reasons.push(first + " empty leading position(s)");
        if (last < isEmpty.length - 1) reasons.push(isEmpty.length - 1 - last + " empty trailing position(s)");
      }
      if (this.leadingZeros === "forbid" && len > 1 && text[0] === "0") reasons.push("leading zero");
      if (this.pattern && !this.pattern.test(text)) reasons.push("does not match " + pyRepr(this.patternSource));
      if (this.allowed && this.allowed.indexOf(text) < 0) reasons.push("not an allowed value");
      if (this.range !== null) {
        b = bounds(this.range);
        var number = pyNumber(text);
        if (number === undefined) reasons.push("not a number");
        else if ((b[0] !== null && number < b[0]) || (b[1] !== null && number > b[1])) reasons.push(text + " outside " + showBounds(this.range));
      }
      return reasons;
    };

    var NORMALIZERS = ["none", "strip", "digits", "upper", "alnum"];
    function makeNormalizer(spec, name) {
      if (isObject(spec)) {
        if (!("regex" in spec)) throw new Error("check '" + name + "': normalize object needs 'regex'");
        var re = pyRegex(spec.regex, false);
        if (!re) throw new Error("check '" + name + "': invalid normalize regex");
        var group = spec.group === undefined ? 0 : spec.group;
        var groups = new RegExp(re.source + "|").exec("").length - 1;
        if (typeof group === "number" && group > groups) throw new Error("check '" + name + "': regex has no group " + group);
        return function (value) {
          var m = re.exec(value);
          if (!m) return "";
          var g = typeof group === "number" ? m[group] : m.groups && m.groups[group];
          return g || "";
        };
      }
      if (NORMALIZERS.indexOf(spec) < 0) throw new Error("check '" + name + "': normalize must be one of ('none', 'strip', 'digits', 'upper', 'alnum')");
      if (spec === "none")
        return function (v) {
          return v;
        };
      if (spec === "strip")
        return function (v) {
          return v.trim();
        };
      var keep = spec === "digits" ? DIGIT_RE : spec === "alnum" ? ALNUM_RE : null;
      if (spec === "upper")
        return function (v) {
          return v.trim().toUpperCase();
        };
      return function (v) {
        return chars(v)
          .filter(function (c) {
            return keep.test(c);
          })
          .join("");
      };
    }
    function CheckRule(spec) {
      var name = spec.name;
      if (!name) throw new Error("Every entry in 'checks' needs a name");
      this.name = name;
      this.sources = (spec.sources || []).slice();
      if (!this.sources.length) throw new Error("check '" + name + "': 'sources' must list at least one name");
      var self = this;
      if (
        this.sources.some(function (s, i) {
          return self.sources.indexOf(s) !== i;
        })
      )
        throw new Error("check '" + name + "': duplicate sources");
      this.priority = (spec.priority && spec.priority.length ? spec.priority : this.sources).slice();
      var extra = this.priority.filter(function (s) {
        return self.sources.indexOf(s) < 0;
      });
      if (extra.length) throw new Error("check '" + name + "': priority names " + JSON.stringify(extra.sort()) + " are not sources");
      this.sources.forEach(function (s) {
        if (self.priority.indexOf(s) < 0) self.priority.push(s);
      });
      this.output = spec.output || name;
      function opt(key, def) {
        return spec[key] === undefined ? def : spec[key];
      }
      this.onMissing = opt("onMissing", "fallback");
      this.onConflict = opt("onConflict", "prefer");
      this.reviewOnConflict = opt("reviewOnConflict", true);
      this.reviewOnFallback = opt("reviewOnFallback", false);
      this.reviewOnAllMissing = opt("reviewOnAllMissing", true);
      this.skipInvalid = opt("skipInvalid", true);
      this.skipFlagged = opt("skipFlagged", false);
      this.absorbReview = opt("absorbSourceReview", true);
      if (["fallback", "review"].indexOf(this.onMissing) < 0) throw new Error("check '" + name + "': onMissing must be one of ('fallback', 'review')");
      if (["prefer", "review", "error"].indexOf(this.onConflict) < 0) throw new Error("check '" + name + "': onConflict must be one of ('prefer', 'review', 'error')");
      this.normalize = makeNormalizer(opt("normalize", "none"), name);
      this.shadows = this.sources.indexOf(this.output) >= 0;
    }
    // checks.resolve: [value|null, chosen, flags, needsReview]
    function resolveCheck(rule, readings) {
      var present = readings.filter(function (r) {
        return r.usable;
      });
      var flags = [];
      if (!present.length) {
        flags.push("all_sources_missing");
        return [null, null, flags, !!rule.reviewOnAllMissing];
      }
      var chosen = present[0].source, value = present[0].normalized, review = false;
      if (chosen !== rule.priority[0]) {
        flags.push("fallback_used");
        review = rule.onMissing === "review" || !!rule.reviewOnFallback;
      }
      var distinct = {};
      present.forEach(function (r) {
        distinct[r.normalized] = 1;
      });
      if (Object.keys(distinct).length > 1) {
        flags.push("cross_check_failed");
        if (rule.onConflict === "error") return [null, null, flags, true];
        if (rule.onConflict === "review" || rule.reviewOnConflict) review = true;
      }
      return [value, chosen, flags, review];
    }
    function has(obj, key) {
      return Object.prototype.hasOwnProperty.call(obj, key);
    }
    // RuleSet: built once per template; apply() runs per sheet
    function RuleSet(template, validateSpec, checksSpec) {
      var self = this;
      this.globalEmpty = template.emptyValue || "";
      this.customLabels = template.customLabels;
      this.template = template;
      this.zones = {};
      template.zones.forEach(function (z) {
        self.zones[z.name] = z;
      });
      var base = {};
      template.allLabels.forEach(function (n) {
        base[n] = true;
      });
      Object.keys(this.customLabels).forEach(function (n) {
        base[n] = true;
      });
      var specs = (checksSpec || []).concat(fallbackZoneChecks(template));
      var rules = specs.map(function (s) {
        return new CheckRule(s);
      });
      var seenNames = {}, seenOutputs = {};
      rules.forEach(function (rule) {
        if (seenNames[rule.name]) throw new Error("Duplicate check name '" + rule.name + "'");
        seenNames[rule.name] = true;
        if (seenOutputs[rule.output]) throw new Error("Two checks write the same output '" + rule.output + "'");
        seenOutputs[rule.output] = true;
        if (base[rule.output] && !rule.shadows) throw new Error("check '" + rule.name + "': output '" + rule.output + "' already exists; an existing column can only be replaced by a check that reads it");
        if (base[rule.name] && rule.name !== rule.output) throw new Error("check '" + rule.name + "': name is already a field, custom label or zone");
      });
      var byName = {}, outputs = {};
      rules.forEach(function (r) {
        byName[r.name] = r;
        outputs[r.output] = r;
      });
      function known(n) {
        return base[n] || has(outputs, n);
      }
      rules.forEach(function (rule) {
        var renames = {};
        rule.sources = rule.sources.map(function (source) {
          var mapped = source;
          if (!known(mapped) && has(byName, mapped)) mapped = byName[mapped].output;
          if (!known(mapped)) throw new Error("check '" + rule.name + "': unknown source '" + mapped + "' (not a field, custom label, zone or check)");
          renames[source] = mapped;
          return mapped;
        });
        rule.priority = rule.priority.map(function (s) {
          return has(renames, s) ? renames[s] : s;
        });
      });
      this.checks = topologicalOrder(rules, outputs);
      this.validations = {};
      this.validationOrder = [];
      Object.keys(validateSpec || {}).forEach(function (name) {
        var target = name;
        if (!known(name) && has(byName, name)) target = byName[name].output;
        if (!known(target)) throw new Error("validate: unknown name '" + name + "' (not a field, custom label, zone or check output)");
        if (!has(self.validations, target)) self.validationOrder.push(target);
        self.validations[target] = new ValidationRule(target, validateSpec[name]);
      });
      this.outputs = outputs;
      this.newOutputColumns = this.checks
        .filter(function (r) {
          return !base[r.output];
        })
        .map(function (r) {
          return r.output;
        });
      this.active = !!(this.checks.length || this.validationOrder.length);
    }
    // Barcode zone option "fallbackZone": a check with another zone as fallback
    function fallbackZoneChecks(template) {
      var zones = {}, specs = [];
      template.zones.forEach(function (z) {
        zones[z.name] = z;
      });
      template.zones.forEach(function (zone) {
        var fallback = zone.options.fallbackZone;
        if (!fallback) return;
        if (!has(zones, fallback) || fallback === zone.name) throw new Error("Zone '" + zone.name + "': fallbackZone '" + fallback + "' is not another zone");
        if (zone.options.lazy !== undefined && zone.options.lazy !== null && zone.options.lazy) throw new Error("Zone '" + zone.name + "' has a fallback and can't be lazy");
        var target = zones[fallback];
        if (target.options.lazy === undefined ? true : target.options.lazy) target.lazy = true;
        specs.push({
          name: zone.name,
          sources: [zone.name, fallback],
          priority: [zone.name, fallback],
          normalize: zone.options.fallbackNormalize === undefined ? "strip" : zone.options.fallbackNormalize,
          onMissing: "fallback",
          onConflict: "prefer",
          reviewOnConflict: false,
          reviewOnFallback: zone.options.reviewOnFallback === undefined ? true : zone.options.reviewOnFallback,
          output: zone.name,
        });
      });
      return specs;
    }
    function topologicalOrder(rules, outputs) {
      var deps = {}, byName = {}, order = [], done = {}, visiting = [];
      rules.forEach(function (rule) {
        byName[rule.name] = rule;
        var d = {};
        rule.sources.forEach(function (s) {
          if (has(outputs, s) && outputs[s] !== rule) d[outputs[s].name] = true;
        });
        deps[rule.name] = Object.keys(d).sort();
      });
      function visit(name) {
        if (done[name]) return;
        var at = visiting.indexOf(name);
        if (at >= 0) throw new Error("checks form a cycle: " + visiting.slice(at).concat([name]).join(" -> "));
        visiting.push(name);
        deps[name].forEach(visit);
        visiting.pop();
        done[name] = true;
        order.push(byName[name]);
      }
      rules.forEach(function (r) {
        visit(r.name);
      });
      return order;
    }
    function rememberEntity(entity) {
      if (!has(entity, "pre_rules")) entity.pre_rules = { flags: (entity.flags || []).slice(), needs_review: !!entity.needs_review };
    }
    // apply(): resolves to [checks, validation, reviewItems]; readLazy(name) -> Promise<zone dict>
    RuleSet.prototype.apply = function (omr, responses, fields, zones, readLazy) {
      if (!this.active) return Promise.resolve([{}, {}, []]);
      return new RuleRun(this, omr, responses, fields, zones, readLazy).run();
    };
    function RuleRun(rules, omr, responses, fields, zones, readLazy) {
      this.rules = rules;
      this.omr = omr;
      this.responses = responses;
      this.fields = fields;
      this.zones = zones;
      this.readLazy = readLazy;
      this.overrides = {};
      this.checkValues = {};
      this.checks = {};
      this.validation = {};
      this.review = [];
      this.held = {};
    }
    RuleRun.prototype.emptyOf = function (name) {
      var z = this.rules.zones[name];
      return z ? z.emptyValue : this.rules.globalEmpty;
    };
    RuleRun.prototype.get = function (name) {
      return has(this.omr, name) ? this.omr[name] : "";
    };
    RuleRun.prototype.baseValue = function (name) {
      var self = this;
      if (has(this.overrides, name)) return this.overrides[name];
      if (has(this.checkValues, name)) return this.checkValues[name];
      var groupOpts = has(this.rules.customLabels, name) ? groupOptionsFor(this.rules.template, name) : null;
      if (groupOpts) return joinGroup(this.rules.customLabels[name], this.omr, groupOpts, this.fields, this.rules.globalEmpty)[0];
      if (has(this.rules.customLabels, name))
        return this.rules.customLabels[name]
          .map(function (c) {
            return pyStr(self.get(c));
          })
          .join("");
      if (has(this.zones, name)) return has(this.zones[name], "value") ? this.zones[name].value : "";
      return this.get(name);
    };
    RuleRun.prototype.columnEmpty = function (label) {
      var field = this.fields[label], value = pyStr(this.get(label));
      if (field && (field.flags || []).indexOf("empty") >= 0) return true;
      return isBlank(value) || value === this.rules.globalEmpty;
    };
    RuleRun.prototype.columns = function (name) {
      var self = this;
      if (has(this.checkValues, name) || has(this.overrides, name)) return [null, null];
      if (has(this.rules.customLabels, name)) {
        var cols = this.rules.customLabels[name];
        return [
          cols.map(function (c) {
            return self.get(c);
          }),
          cols.map(function (c) {
            return self.columnEmpty(c);
          }),
        ];
      }
      var zone = this.zones[name];
      if (zone) {
        var characters = (zone.details || {}).characters;
        if (characters && characters.length) return [characters, characters.map(isBlank)];
      }
      return [null, null];
    };
    RuleRun.prototype.isMissing = function (name, value) {
      var self = this;
      value = pyStr(value);
      if (isBlank(value) || value === this.emptyOf(name)) return true;
      if (has(this.rules.customLabels, name) && !has(this.overrides, name))
        return this.rules.customLabels[name].every(function (c) {
          return self.columnEmpty(c);
        });
      if (has(this.fields, name) && !has(this.overrides, name)) return (this.fields[name].flags || []).indexOf("empty") >= 0;
      return false;
    };
    RuleRun.prototype.entity = function (name) {
      if (has(this.checkValues, name)) return null;
      return this.fields[name] || this.zones[name] || null;
    };
    RuleRun.prototype.setValue = function (name, value) {
      var self = this;
      this.overrides[name] = value;
      if (has(this.responses, name) || has(this.rules.outputs, name)) this.responses[name] = value;
      if (has(this.omr, name) && !has(this.rules.customLabels, name)) {
        this.omr[name] = value;
        Object.keys(this.rules.customLabels).forEach(function (label) {
          if (self.rules.customLabels[label].indexOf(name) >= 0 && !has(self.overrides, label)) self.responses[label] = self.baseValue(label);
        });
      }
    };
    RuleRun.prototype.run = function () {
      var self = this, rules = this.rules;
      var pending = rules.validationOrder.filter(function (n) {
        return !has(rules.outputs, n);
      });
      // columns and zones first, so blanked columns show in their custom labels
      pending
        .filter(function (n) {
          return !has(rules.customLabels, n);
        })
        .concat(
          pending.filter(function (n) {
            return has(rules.customLabels, n);
          })
        )
        .forEach(function (n) {
          self.validate(n, null);
        });
      // Groups with placeholders show columns their validation flagged
      Object.keys(rules.customLabels).forEach(function (label) {
        if (has(self.overrides, label) || has(rules.outputs, label)) return;
        if (groupOptionsFor(rules.template, label) && has(self.responses, label)) self.responses[label] = self.baseValue(label);
      });
      var chain = Promise.resolve();
      rules.checks.forEach(function (rule) {
        chain = chain.then(function () {
          return self.runCheck(rule);
        });
      });
      return chain.then(function () {
        return [self.checks, self.validation, self.review];
      });
    };
    RuleRun.prototype.validate = function (name, record) {
      var rule = this.rules.validations[name], value = this.baseValue(name), cols = this.columns(name);
      var reasons = rule.failures(value, cols[0], cols[1]);
      var kind = record ? "check" : has(this.rules.customLabels, name) ? "custom_label" : has(this.zones, name) ? "zone" : "field";
      this.validation[name] = { ok: !reasons.length, kind: kind, value: value, reasons: reasons, action: reasons.length ? rule.onFail : null };
      if (!reasons.length) return;
      if (rule.blanks) this.setValue(name, this.emptyOf(name));
      if (record) {
        record.flags.push(VALIDATION_FLAG);
        record.validation_reasons = reasons;
        if (rule.blanks) record.value = this.emptyOf(name);
        if (rule.reviews) record.needs_review = true;
        return;
      }
      var entity = this.entity(name);
      if (entity) {
        rememberEntity(entity);
        var flags = {};
        (entity.flags || []).concat([VALIDATION_FLAG]).forEach(function (f) {
          flags[f] = 1;
        });
        entity.flags = Object.keys(flags).sort();
        entity.validation_reasons = reasons;
        if (rule.reviews) {
          entity.needs_review = true;
          this.held[name] = true;
        }
      } else if (rule.reviews) {
        var item = { kind: kind, name: name, flags: [VALIDATION_FLAG], reasons: reasons };
        if (kind === "custom_label") item.fields = this.rules.customLabels[name].slice();
        this.review.push(item);
      }
    };
    // [raw, normalized, usable, note] for one source (may read a lazy zone first)
    RuleRun.prototype.sourceReading = function (rule, source, haveValue) {
      var self = this, zone = this.zones[source];
      if (zone && !has(this.checkValues, source) && (zone.flags || []).indexOf("not_read") >= 0) {
        if (haveValue || !this.readLazy) return Promise.resolve([null, null, false, "not_read"]);
        return Promise.resolve(this.readLazy(source)).then(function (read) {
          self.zones[source] = read;
          var v = has(read, "value") ? read.value : "";
          if (has(self.responses, source) && !has(self.overrides, source)) self.responses[source] = v;
          if (has(self.omr, source)) self.omr[source] = v;
          return self.reading(rule, source);
        });
      }
      return Promise.resolve(this.reading(rule, source));
    };
    RuleRun.prototype.reading = function (rule, source) {
      var raw = pyStr(this.baseValue(source)), normalized = rule.normalize(raw);
      if (this.isMissing(source, raw) || !normalized) return [raw, normalized, false, "missing"];
      var validation = this.rules.validations[source];
      if (rule.skipInvalid && validation) {
        var invalid;
        if (has(this.validation, source) && !has(this.checkValues, source)) invalid = !this.validation[source].ok;
        else {
          var cols = this.columns(source);
          invalid = validation.failures(raw, cols[0], cols[1]).length > 0;
        }
        if (invalid) return [raw, normalized, false, "invalid"];
      }
      var entity = this.entity(source);
      if (rule.skipFlagged && entity && entity.needs_review) return [raw, normalized, false, "flagged"];
      return [raw, normalized, true, null];
    };
    RuleRun.prototype.runCheck = function (rule) {
      var self = this, readings = [], notes = {}, haveValue = false, chain = Promise.resolve();
      rule.priority.forEach(function (source) {
        chain = chain.then(function () {
          return self.sourceReading(rule, source, haveValue).then(function (r) {
            readings.push({ source: source, raw: r[0], normalized: r[1], usable: r[2] });
            if (r[3]) notes[source] = r[3];
            haveValue = haveValue || r[2];
          });
        });
      });
      return chain.then(function () {
        var res = resolveCheck(rule, readings), value = res[0];
        var sources = {}, normalized = {};
        readings.forEach(function (r) {
          sources[r.source] = r.raw;
          normalized[r.source] = r.normalized;
        });
        var record = {
          value: value === null ? self.emptyOf(rule.output) : value,
          chosen_source: res[1],
          sources: sources,
          normalized: normalized,
          skipped: notes,
          flags: res[2],
          needs_review: res[3],
          output: rule.output,
        };
        self.checkValues[rule.output] = record.value;
        self.responses[rule.output] = record.value;
        if (has(self.rules.validations, rule.output)) {
          self.validate(rule.output, record);
          self.checkValues[rule.output] = record.value;
          self.responses[rule.output] = record.value;
        }
        var fl = {};
        record.flags.forEach(function (f) {
          fl[f] = 1;
        });
        record.flags = Object.keys(fl).sort();
        if (rule.absorbReview && !record.needs_review && value !== null) self.absorb(rule, readings);
        self.checks[rule.name] = record;
        if (record.needs_review) self.review.push({ kind: "check", name: rule.name, flags: record.flags });
      });
    };
    // A resolved check settles its sources' own review flags when the source was
    // missing or agrees with another present source
    RuleRun.prototype.absorb = function (rule, readings) {
      var self = this;
      var usable = readings.filter(function (r) {
        return r.usable;
      });
      readings.forEach(function (r) {
        var source = r.source;
        if (has(self.checkValues, source) && source !== rule.output) return;
        var entity = self.fields[source] || self.zones[source];
        if (!entity || !entity.needs_review) return;
        if (self.held[source]) return;
        if (r.usable) {
          var agrees = usable.some(function (o) {
            return o.source !== source && o.normalized === r.normalized;
          });
          if (!agrees) return;
        }
        rememberEntity(entity);
        entity.needs_review = false;
        entity.review_resolved_by = rule.name;
      });
    };
    // review_items: flagged fields, flagged zones, then rule items
    function reviewItems(fields, zones, extra) {
      var out = [];
      Object.keys(fields).forEach(function (name) {
        if (fields[name].needs_review) out.push({ kind: "field", name: name, flags: fields[name].flags });
      });
      Object.keys(zones).forEach(function (name) {
        if (zones[name].needs_review) out.push({ kind: "zone", name: name, flags: zones[name].flags });
      });
      return out.concat(extra || []);
    }

    // ------------------------------------------------------------------------
    // Image decoding (browser and Node)
    // ------------------------------------------------------------------------
    function makeCanvas(w, h) {
      if (typeof OffscreenCanvas !== "undefined") {
        try {
          var oc = new OffscreenCanvas(w, h);
          if (oc.getContext("2d")) return oc;
        } catch (e) {
          /* fall back to DOM canvas */
        }
      }
      if (typeof document !== "undefined") {
        var c = document.createElement("canvas");
        c.width = w;
        c.height = h;
        return c;
      }
      throw new Error("No canvas available to decode the image in this environment");
    }
    function drawableToPixels(drawable, sw, sh, maxDim, color) {
      var scale = maxDim && Math.max(sw, sh) > maxDim ? maxDim / Math.max(sw, sh) : 1;
      var w = Math.max(1, Math.round(sw * scale)), h = Math.max(1, Math.round(sh * scale));
      var canvas = makeCanvas(w, h), ctx = canvas.getContext("2d");
      if ("imageSmoothingQuality" in ctx) ctx.imageSmoothingQuality = "high";
      ctx.drawImage(drawable, 0, 0, w, h);
      var data = ctx.getImageData(0, 0, w, h);
      return color ? { width: w, height: h, data: data.data, channels: 4 } : rgbaToGray(data.data, w, h);
    }
    function blobToDrawable(blob) {
      if (typeof createImageBitmap === "function") {
        return createImageBitmap(blob, { imageOrientation: "from-image" }).catch(function () {
          return createImageBitmap(blob);
        });
      }
      return new Promise(function (resolve, reject) {
        var url = URL.createObjectURL(blob), img = new Image();
        img.onload = function () {
          URL.revokeObjectURL(url);
          resolve(img);
        };
        img.onerror = function () {
          URL.revokeObjectURL(url);
          reject(new Error("Could not decode image"));
        };
        img.src = url;
      });
    }
    // Convert any supported source into a gray image, or with options.color into
    // RGB(A) pixels {width, height, data, channels} when the source has colour.
    // options.maxDimension caps the longest side (phone photos are downsampled by
    // the browser's own resampler).
    function decodeImage(source, options) {
      options = options || {};
      var maxDim = options.maxDimension === undefined ? 3000 : options.maxDimension, color = !!options.color;
      return Promise.resolve().then(function () {
        if (!source) throw new Error("No image given");
        if (source.data && source.width && source.height && !(typeof HTMLCanvasElement !== "undefined" && source instanceof HTMLCanvasElement)) {
          var n = source.width * source.height, len = source.data.length;
          if (len === n) return makeImage(source.width, source.height, source.data instanceof Uint8Array ? source.data : new Uint8Array(source.data));
          if (len === n * 4 || len === n * 3) {
            var px = { width: source.width, height: source.height, data: source.data, channels: len / n };
            return color ? px : pixelsToGray(px);
          }
          throw new Error("Unsupported pixel buffer: expected 1, 3 or 4 channels");
        }
        if (typeof Blob !== "undefined" && source instanceof Blob) {
          return blobToDrawable(source).then(function (d) {
            var g = drawableToPixels(d, d.width || d.naturalWidth, d.height || d.naturalHeight, maxDim, color);
            if (d.close) d.close();
            return g;
          });
        }
        if (typeof HTMLImageElement !== "undefined" && source instanceof HTMLImageElement) {
          var ready = source.complete && source.naturalWidth ? Promise.resolve() : source.decode ? source.decode() : new Promise(function (res, rej) {
            source.onload = res;
            source.onerror = rej;
          });
          return ready.then(function () {
            return drawableToPixels(source, source.naturalWidth, source.naturalHeight, maxDim, color);
          });
        }
        if (typeof HTMLVideoElement !== "undefined" && source instanceof HTMLVideoElement) return drawableToPixels(source, source.videoWidth, source.videoHeight, maxDim, color);
        if (source.getContext && source.width) {
          if (maxDim && Math.max(source.width, source.height) > maxDim) return drawableToPixels(source, source.width, source.height, maxDim, color);
          var cd = source.getContext("2d").getImageData(0, 0, source.width, source.height);
          return color ? { width: source.width, height: source.height, data: cd.data, channels: 4 } : rgbaToGray(cd.data, source.width, source.height);
        }
        if (source.width && source.height && typeof ImageBitmap !== "undefined" && source instanceof ImageBitmap) return drawableToPixels(source, source.width, source.height, maxDim, color);
        throw new Error("Unsupported image source");
      });
    }

    // ------------------------------------------------------------------------
    // Engine
    // ------------------------------------------------------------------------
    function buildPreprocessor(spec, template, config, assets) {
      var o = spec.options || {};
      switch (spec.name) {
        case "TimingMarkAlignment":
          return new TimingMarkAlignment(o, template.pageDimensions);
        case "CropPage":
          return new CropPage(o);
        case "CropOnMarkers":
          return new CropOnMarkers(o, assets[o.relativePath || "omr_marker.jpg"], config);
        case "Levels":
          return new LevelsProcessor(o);
        case "MedianBlur":
          return new MedianBlurProcessor(o);
        case "GaussianBlur":
          return new GaussianBlurProcessor(o);
        default:
          var err = new Error("Preprocessor '" + spec.name + "' is not supported in the browser engine; process this template on the server");
          err.code = "unsupported_preprocessor";
          throw err;
      }
    }

    function Engine(template, config, preProcessors, options) {
      this.template = template;
      this.config = config;
      this.preProcessors = preProcessors;
      this.options = options || {};
      this.bubbleModel = null;
      this.zoneReaders = {};
      this.evaluation = this.options.evaluation || null;
    }
    Engine.prototype.setZoneReader = function (type, fn) {
      this.zoneReaders[type] = fn;
      return this;
    };
    Engine.prototype.loadBubbleModel = function (opts) {
      var self = this;
      return loadBubbleModel(opts).then(function (model) {
        self.bubbleModel = model;
        return model;
      });
    };
    Engine.prototype.setBubbleModel = function (model) {
      this.bubbleModel = model;
      return this;
    };
    // Change the page colour dropout (object, mode string or null for grey)
    Engine.prototype.setColorDropout = function (spec) {
      this.template.colorDropout = normalizeDropout(spec);
      return this;
    };
    // Distinct per-zone dropout settings that differ from the page's
    Engine.prototype.zoneDropoutSpecs = function () {
      var page = this.template.colorDropout, specs = [], keys = {};
      this.template.zones.forEach(function (z) {
        var spec = z.colorDropout;
        if (spec && spec.key !== page.key && !keys[spec.key]) {
          keys[spec.key] = true;
          specs.push(spec);
        }
      });
      return specs;
    };
    // True when photos should be decoded in colour (some dropout isn't plain grey)
    Object.defineProperty(Engine.prototype, "needsColor", {
      get: function () {
        return this.template.colorDropout.mode !== "grey" || this.zoneDropoutSpecs().length > 0;
      },
    });
    // Template.prepare_image: pixels -> {gray, variants {spec key: grey image}}
    Engine.prototype.prepareImage = function (px) {
      if (!px.channels || px.channels === 1) return { gray: px.channels ? makeImage(px.width, px.height, px.data) : px, variants: null };
      var variants = null, grey = null;
      function greyOf() {
        return (grey = grey || pixelsToGray(px));
      }
      this.zoneDropoutSpecs().forEach(function (spec) {
        variants = variants || {};
        variants[spec.key] = applyDropout(px, spec, greyOf);
      });
      return { gray: applyDropout(px, this.template.colorDropout, greyOf), variants: variants };
    };
    // Registration + preprocessing (ImageInstanceOps.apply_preprocessors). Companion
    // images (colour dropout variants) follow the same geometry, updated in place.
    Engine.prototype.register = function (gray, ctx, companions) {
      var pre = this.preProcessors, dims = this.config.dimensions, img = gray, key;
      if (pre.length && !pre[0].needsFullResolution) {
        var pw = Math.trunc(dims.processing_width), ph = Math.trunc(dims.processing_height);
        img = resizeLinear(img, pw, ph);
        for (key in companions || {}) companions[key] = resizeLinear(companions[key], pw, ph);
      }
      for (var i = 0; i < pre.length; i++) {
        ctx.geometry = companions ? [] : null;
        img = pre[i].apply(img, ctx);
        var ops = ctx.geometry;
        ctx.geometry = null;
        if (!img) return null;
        if (companions && pre[i].geometry !== "none") {
          for (key in companions) {
            var c = companions[key];
            for (var k = 0; k < ops.length && c; k++) c = ops[k](c);
            if (c) companions[key] = c;
            else delete companions[key];
          }
        }
      }
      return img;
    };
    // Live camera preview: register one (small) gray frame without reading it.
    // Returns where the page and its timing marks sit in the frame, and how sharp it is.
    Engine.prototype.preview = function (gray) {
      var ctx = {}, pre = this.preProcessors, dims = this.config.dimensions, sx = 1, sy = 1;
      if (pre.length && !pre[0].needsFullResolution) {
        sx = Math.trunc(dims.processing_width) / gray.width;
        sy = Math.trunc(dims.processing_height) / gray.height;
      }
      var aligned = null;
      try {
        aligned = this.register(gray, ctx, null);
      } catch (error) {
        ctx.registration = { error: String((error && error.message) || error) };
      }
      var reg = ctx.registration || {}, page = this.template.pageDimensions, quad = null, marks = null;
      var blocks = null, bubbles = null, bubbleRadius = null;
      function toFrame(p) {
        return [p[0] / sx, p[1] / sy];
      }
      if (reg.homography) {
        quad = projectPoints(reg.homography, [[0, 0], [page[0], 0], [page[0], page[1]], [0, page[1]]]).map(toFrame);
        var tm = pre.filter(function (p) {
          return p.name === "TimingMarkAlignment";
        })[0];
        if (tm) marks = projectPoints(reg.homography, tm.expected).map(toFrame);
        var layout = this.previewLayout();
        blocks = layout.blocks.map(function (corners) {
          return projectPoints(reg.homography, corners).map(toFrame);
        });
        bubbles = projectPoints(reg.homography, layout.bubbles).map(toFrame);
        // bubble radius in frame pixels, from the page width as seen in the frame
        var top = Math.hypot(quad[1][0] - quad[0][0], quad[1][1] - quad[0][1]);
        bubbleRadius = (layout.radius * top) / page[0];
      } else if (reg.corners && reg.corners.length === 4) {
        quad = reg.corners.map(toFrame);
      }
      return {
        ok: !!aligned,
        error: reg.error || null,
        method: reg.method || null,
        matched: reg.matched_marks !== undefined ? reg.matched_marks : null,
        expected: reg.expected_marks !== undefined ? reg.expected_marks : null,
        quad: quad,
        marks: marks,
        blocks: blocks,
        bubbles: bubbles,
        bubbleRadius: bubbleRadius,
        sharpness: roundTo(laplacianVariance(gray), 1),
      };
    };
    // Field block outlines and bubble centres in template units, for drawing
    // the template over a camera frame (computed once per engine)
    Engine.prototype.previewLayout = function () {
      if (this._previewLayout) return this._previewLayout;
      var blocks = [], bubbles = [], sizes = [];
      this.template.fieldBlocks.forEach(function (block) {
        var bw = block.bubbleDimensions[0], bh = block.bubbleDimensions[1];
        var x0 = block.origin[0], y0 = block.origin[1];
        var x1 = x0 + block.dimensions[0], y1 = y0 + block.dimensions[1];
        blocks.push([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]);
        block.fields.forEach(function (field) {
          field.bubbles.forEach(function (b) {
            bubbles.push([b.x + bw / 2, b.y + bh / 2]);
          });
        });
        sizes.push(Math.min(bw, bh));
      });
      sizes.sort(function (a, b) { return a - b; });
      var radius = sizes.length ? sizes[sizes.length >> 1] * 0.35 : 0;
      this._previewLayout = { blocks: blocks, bubbles: bubbles, radius: radius };
      return this._previewLayout;
    };
    // Variance of the 4-neighbour Laplacian: low on blurred or shaken frames
    function laplacianVariance(img) {
      var w = img.width, h = img.height, d = img.data, sum = 0, sq = 0, n = 0;
      for (var y = 1; y < h - 1; y += 2) {
        for (var x = 1; x < w - 1; x += 2) {
          var i = y * w + x, v = d[i - 1] + d[i + 1] + d[i - w] + d[i + w] - 4 * d[i];
          sum += v;
          sq += v * v;
          n++;
        }
      }
      if (!n) return 0;
      var mean = sum / n;
      return sq / n - mean * mean;
    }
    Engine.prototype.scan = function (source, opts) {
      var self = this;
      opts = opts || {};
      var fileId = opts.fileId || "image";
      var started = now(), timings = {};
      return decodeImage(source, { maxDimension: opts.maxDimension !== undefined ? opts.maxDimension : this.options.maxDimension, color: this.needsColor })
        .then(function (pixels) {
          timings.decode = elapsed(started);
          return self.scanPixels(pixels, fileId, started, timings);
        })
        .catch(function (error) {
          return makeResult(fileId, STATUS_ERROR, { error: String((error && error.message) || error), timings_ms: timings });
        });
    };
    // A gray image or colour pixels {width, height, data, channels: 3 | 4} (RGB order)
    Engine.prototype.scanGray = function (gray, fileId, started, timings) {
      return this.scanPixels(gray, fileId, started, timings);
    };
    Engine.prototype.scanPixels = function (pixels, fileId, started, timings) {
      var self = this, template = this.template, config = this.config;
      started = started || now();
      timings = timings || {};
      var gray = pixels, variants = null;
      if (pixels.channels && pixels.channels > 1) {
        // colour dropout; variants only for zones with their own setting
        var dropStart = now(), prepared = this.prepareImage(pixels);
        gray = prepared.gray;
        variants = prepared.variants;
        timings.dropout = elapsed(dropStart);
      }
      var step = now(), ctx = {};
      var aligned = this.register(gray, ctx, variants);
      timings.registration = elapsed(step);
      if (!aligned) {
        timings.total = elapsed(started);
        var r = makeResult(fileId, STATUS_ERROR, { error: REGISTRATION_ERROR, timings_ms: timings });
        hide(r, "registration", ctx.registration || null);
        return Promise.resolve(r);
      }
      step = now();
      var model = this.bubbleModel, barcodeParams = config.barcode_params || {};
      return readBubbles(template, aligned, config, model ? model.predictMarked : null).then(function (detailed) {
        timings.bubbles = elapsed(step);
        var zstep = now(), alignedImage = detailed.alignedImage, readyVariants = {};
        // A zone with its own colorDropout reads its variant, resized and normalised like the page
        function zoneImage(zone) {
          var spec = zone.colorDropout;
          if (!variants || !spec || !variants[spec.key]) return alignedImage;
          if (!readyVariants[spec.key]) {
            var v = resizeLinear(variants[spec.key], template.pageDimensions[0], template.pageDimensions[1]), mm = minMax(v);
            readyVariants[spec.key] = mm[1] > mm[0] ? normalizeMinMax(v) : v;
          }
          return readyVariants[spec.key];
        }
        function read(zone) {
          return readZone(zone, zoneImage(zone), self.zoneReaders, barcodeParams);
        }
        return Promise.all(
          template.zones.map(function (z) {
            // lazy fallback zones are read later, only if a check needs them
            return z.lazy ? skippedZone(z) : read(z);
          })
        ).then(function (zoneList) {
          timings.zones = elapsed(zstep);
          var omr = Object.assign({}, detailed.omrResponse), zones = {};
          zoneList.forEach(function (z) {
            omr[z.name] = z.value;
            zones[z.name] = z;
          });
          var responses = {};
          var fields = detailed.fieldDetails, rstep = now();
          Object.keys(template.customLabels).forEach(function (label) {
            var options = groupOptionsFor(template, label);
            responses[label] = options
              ? joinGroup(template.customLabels[label], omr, options, fields, template.emptyValue || "")[0]
              : template.customLabels[label]
                  .map(function (k) {
                    return omr[k];
                  })
                  .join("");
          });
          template.nonCustomLabels.forEach(function (label) {
            responses[label] = omr[label];
          });
          var byName = {};
          template.zones.forEach(function (z) {
            byName[z.name] = z;
          });
          return template.rules
            .apply(omr, responses, fields, zones, function (name) {
              return read(byName[name]);
            })
            .then(function (ruleOut) {
              if (template.rules.active) timings.rules = elapsed(rstep);
              var score = self.evaluation ? self.evaluation(responses) : null;
              var review = reviewItems(fields, zones, ruleOut[2]);
              var groups = describeGroups(omr, template, fields);
              review = review.concat(groupReviewItems(groups, review)).concat(sheetReview(fields, config.review_params));
              timings.total = elapsed(started);
              var result = makeResult(fileId, review.length ? STATUS_NEEDS_REVIEW : STATUS_OK, {
                responses: responses,
                fields: fields,
                zones: zones,
                review: review,
                score: score,
                thresholds: detailed.thresholds,
                timings_ms: timings,
                checks: ruleOut[0],
                validation: ruleOut[1],
              });
              if (Object.keys(groups).length) result.groups = groups;
              hide(result, "alignedImage", alignedImage);
              hide(result, "registration", ctx.registration || null);
              return result;
            });
        });
      });
    };
    // Sheet-level review items (review_params.min_marked_bubbles; 0 = off)
    function sheetReview(fields, params) {
      var minimum = (params && params.min_marked_bubbles) || 0;
      if (!minimum) return [];
      var marked = 0;
      Object.keys(fields).forEach(function (k) {
        fields[k].bubbles.forEach(function (b) {
          if (b.marked) marked++;
        });
      });
      if (marked >= minimum) return [];
      return [{ kind: "sheet", name: "too_few_marks", flags: ["too_few_marks"], marked_bubbles: marked, min_marked_bubbles: minimum }];
    }
    function hide(obj, key, value) {
      Object.defineProperty(obj, key, { value: value, enumerable: false, writable: true, configurable: true });
    }
    function makeResult(fileId, status, parts) {
      parts = parts || {};
      return {
        file_id: fileId,
        status: status,
        responses: parts.responses || {},
        fields: parts.fields || {},
        zones: parts.zones || {},
        review: parts.review || [],
        score: parts.score === undefined ? null : parts.score,
        error: parts.error || null,
        thresholds: parts.thresholds || {},
        timings_ms: parts.timings_ms || {},
        checks: parts.checks || {},
        validation: parts.validation || {},
      };
    }

    // Accepts a template.json object, its JSON text, or the API's GET /templates/{id}
    // payload ({template, config, ...}). Resolves to an Engine.
    function loadTemplate(templateJson, options) {
      options = options || {};
      return Promise.resolve()
        .then(function () {
          var t = typeof templateJson === "string" ? JSON.parse(templateJson) : templateJson;
          var config = options.config;
          if (t && t.template && t.template.fieldBlocks) {
            if (!config && t.config) config = t.config;
            t = t.template;
          }
          var cfg = deepMerge(CONFIG_DEFAULTS, config || {});
          // templateOverrides replaces top-level template keys (null removes one), e.g.
          // a regrade with another colorDropout
          var overrides = options.templateOverrides;
          if (overrides) {
            t = Object.assign({}, t);
            Object.keys(overrides).forEach(function (k) {
              if (overrides[k] === null) delete t[k];
              else t[k] = JSON.parse(JSON.stringify(overrides[k]));
            });
          }
          var template = parseTemplate(t);
          var needed = [];
          (template.preProcessors || []).forEach(function (p) {
            if (p.name === "CropOnMarkers") needed.push((p.options && p.options.relativePath) || "omr_marker.jpg");
          });
          var assets = Object.assign({}, options.assets || {});
          return Promise.all(
            needed.map(function (name) {
              var a = assets[name];
              if (a) return decodeImage(a, { maxDimension: 0 }).then(function (g) {
                assets[name] = g;
              });
              if (options.assetsBaseUrl && typeof fetch === "function") {
                var base = options.assetsBaseUrl.replace(/\/?$/, "/");
                return fetch(base + name)
                  .then(function (r) {
                    if (!r.ok) throw new Error("Failed to fetch " + name);
                    return r.blob();
                  })
                  .then(function (b) {
                    return decodeImage(b, { maxDimension: 0 });
                  })
                  .then(function (g) {
                    assets[name] = g;
                  });
              }
              return null;
            })
          ).then(function () {
            var pre = template.preProcessors.map(function (p) {
              return buildPreprocessor(p, template, cfg, assets);
            });
            return new Engine(template, cfg, pre, options);
          });
        });
    }

    // Template support check without building an engine
    function checkTemplate(templateJson) {
      var t = typeof templateJson === "string" ? JSON.parse(templateJson) : templateJson;
      if (t && t.template && t.template.fieldBlocks) t = t.template;
      var issues = [];
      (t.preProcessors || []).forEach(function (p) {
        if (["TimingMarkAlignment", "CropPage", "CropOnMarkers", "Levels", "MedianBlur", "GaussianBlur"].indexOf(p.name) < 0) issues.push("Preprocessor '" + p.name + "' is not supported in the browser");
        if (p.name === "TimingMarkAlignment" && p.options && p.options.nonRigid) issues.push("nonRigid thin-plate correction runs (port) - slower on phones");
      });
      Object.keys(t.zones || {}).forEach(function (n) {
        var z = t.zones[n];
        if ((z.type === "barcode" || z.type === "qrcode") && !zxing.lib) {
          var o = z.options || {}, builtin = z.type === "barcode" && Object.keys(builtinFormats(o.formats)).length && (o.engines || BARCODE_ENGINES).indexOf("builtin") >= 0;
          if (!builtin) issues.push("Zone '" + n + "' needs OMR.enableBarcodes()");
        }
        if ((z.type === "ocr" || z.type === "icr") && !zoneReaders[z.type]) issues.push("Zone '" + n + "' (" + z.type + ") has no reader registered; it will be flagged engine_unavailable");
      });
      return { supported: !issues.some(function (s) {
        return s.indexOf("not supported") >= 0;
      }), issues: issues };
    }

    // ------------------------------------------------------------------------
    // Drawing helpers for UIs
    // ------------------------------------------------------------------------
    function renderResult(canvas, result, options) {
      options = options || {};
      var img = options.image || result.alignedImage;
      if (!img) return null;
      canvas.width = img.width;
      canvas.height = img.height;
      var ctx = canvas.getContext("2d");
      var id = ctx.createImageData(img.width, img.height), n = img.width * img.height;
      for (var i = 0, j = 0; i < n; i++, j += 4) {
        id.data[j] = id.data[j + 1] = id.data[j + 2] = img.data[i];
        id.data[j + 3] = 255;
      }
      ctx.putImageData(id, 0, 0);
      var lw = Math.max(2, Math.round(img.width / 500));
      ctx.lineWidth = lw;
      Object.keys(result.fields || {}).forEach(function (label) {
        var f = result.fields[label];
        f.bubbles.forEach(function (b) {
          if (b.marked) {
            ctx.fillStyle = f.needs_review ? "rgba(234,88,12,0.45)" : "rgba(22,163,74,0.45)";
            ctx.fillRect(b.x, b.y, b.w, b.h);
          }
          if (f.needs_review) {
            ctx.strokeStyle = "rgba(234,88,12,0.9)";
            ctx.strokeRect(b.x + 1, b.y + 1, b.w - 2, b.h - 2);
          } else if (options.showAll) {
            ctx.strokeStyle = "rgba(37,99,235,0.35)";
            ctx.strokeRect(b.x + 1, b.y + 1, b.w - 2, b.h - 2);
          }
        });
      });
      Object.keys(result.zones || {}).forEach(function (name) {
        var z = result.zones[name], b = z.box;
        ctx.strokeStyle = z.needs_review ? "rgba(234,88,12,0.9)" : "rgba(37,99,235,0.9)";
        ctx.strokeRect(b[0], b[1], b[2], b[3]);
        ctx.fillStyle = ctx.strokeStyle;
        ctx.font = Math.max(14, Math.round(img.width / 70)) + "px sans-serif";
        ctx.fillText(name + ": " + (z.value || "—"), b[0] + 4, Math.max(b[1] - 6, 14));
      });
      return canvas;
    }

    // ------------------------------------------------------------------------
    // Worker-backed engine
    // ------------------------------------------------------------------------
    function createWorkerEngine(templateJson, options) {
      options = options || {};
      var url = options.workerUrl || "omr.worker.js";
      var worker = new Worker(url);
      var seq = 0, pending = {};
      function call(type, payload, transfer) {
        return new Promise(function (resolve, reject) {
          var id = ++seq;
          pending[id] = { resolve: resolve, reject: reject };
          worker.postMessage({ id: id, type: type, payload: payload }, transfer || []);
        });
      }
      worker.onmessage = function (e) {
        var m = e.data, p = pending[m.id];
        if (!p) return;
        delete pending[m.id];
        if (m.error) p.reject(new Error(m.error));
        else p.resolve(m.result);
      };
      worker.onerror = function (e) {
        Object.keys(pending).forEach(function (k) {
          pending[k].reject(new Error(e.message || "Worker error"));
          delete pending[k];
        });
      };
      var initOptions = { config: options.config, maxDimension: options.maxDimension, omrScriptUrl: options.omrScriptUrl, extraScripts: options.extraScripts || [], barcodes: options.barcodes || null, bubbleModel: options.bubbleModel || null, templateOverrides: options.templateOverrides || null };
      var assets = options.assets || {};
      var assetsReady = Promise.all(
        Object.keys(assets).map(function (name) {
          return decodeImage(assets[name], { maxDimension: 0 }).then(function (g) {
            return [name, g];
          });
        })
      ).then(function (pairs) {
        var out = {};
        pairs.forEach(function (p) {
          out[p[0]] = p[1];
        });
        return out;
      });
      var ready = assetsReady.then(function (decoded) {
        initOptions.assets = decoded;
        initOptions.assetsBaseUrl = options.assetsBaseUrl;
        return call("init", { template: typeof templateJson === "string" ? JSON.parse(templateJson) : templateJson, options: initOptions });
      });
      return ready.then(function (info) {
        return {
          info: info,
          worker: worker,
          // True when photos are decoded in colour (a colour dropout is set)
          get needsColor() {
            return !!info.needsColor;
          },
          setColorDropout: function (spec) {
            return call("setColorDropout", { spec: spec === undefined ? null : spec }).then(function (needsColor) {
              info.needsColor = needsColor;
              return needsColor;
            });
          },
          scan: function (source, opts) {
            opts = opts || {};
            var t0 = now();
            // Colour dropout needs the RGBA pixels; otherwise the page sends gray
            var maxDim = opts.maxDimension !== undefined ? opts.maxDimension : options.maxDimension;
            return decodeImage(source, { maxDimension: maxDim, color: !!info.needsColor }).then(function (px) {
              var decodeMs = elapsed(t0);
              var data = px.data, buf = data.byteOffset === 0 && data.buffer.byteLength === data.byteLength ? data.buffer : data.slice().buffer;
              return call("scan", { width: px.width, height: px.height, channels: px.channels || 1, data: buf, fileId: opts.fileId || "image", returnImage: opts.returnImage !== false }, [buf]).then(function (res) {
                var result = res.result;
                result.timings_ms.decode = decodeMs;
                if (res.image) hide(result, "alignedImage", makeImage(res.image.width, res.image.height, new Uint8Array(res.image.data)));
                hide(result, "registration", res.registration || null);
                return result;
              });
            });
          },
          // gray: {width, height, data} from a downscaled video frame
          preview: function (gray) {
            var data = gray.data, buf = data.byteOffset === 0 && data.buffer.byteLength === data.byteLength ? data.buffer : data.slice().buffer;
            return call("preview", { width: gray.width, height: gray.height, data: buf }, [buf]);
          },
          terminate: function () {
            worker.terminate();
          },
        };
      });
    }

    return {
      VERSION: VERSION,
      CONFIG_DEFAULTS: CONFIG_DEFAULTS,
      FIELD_TYPES: FIELD_TYPES,
      DEFAULT_LIBS: DEFAULT_LIBS,
      loadTemplate: loadTemplate,
      checkTemplate: checkTemplate,
      createWorkerEngine: createWorkerEngine,
      decodeImage: decodeImage,
      enableBarcodes: enableBarcodes,
      barcodesAvailable: barcodesAvailable,
      loadBubbleModel: loadBubbleModel,
      registerZoneReader: function (type, fn) {
        if (fn) zoneReaders[type] = fn;
        else delete zoneReaders[type];
      },
      renderResult: renderResult,
      Engine: Engine,
      // Low-level building blocks (exported for tests and advanced use)
      _internals: {
        parseTemplate: parseTemplate,
        joinGroup: joinGroup,
        columnState: columnState,
        makeImage: makeImage,
        rgbaToGray: rgbaToGray,
        resizeLinear: resizeLinear,
        resizeArea: resizeArea,
        normalizeMinMax: normalizeMinMax,
        gaussianBlur: gaussianBlur,
        otsuValue: otsuValue,
        adaptiveMeanThresholdInv: adaptiveMeanThresholdInv,
        connectedComponents: connectedComponents,
        warpPerspective: warpPerspective,
        ellipseMask: ellipseMask,
        findHomographyLS: findHomographyLS,
        findHomographyRansac: findHomographyRansac,
        getPerspectiveTransform: getPerspectiveTransform,
        findPageCorners: findPageCorners,
        getGlobalThreshold: getGlobalThreshold,
        getLocalThreshold: getLocalThreshold,
        cropImage: cropImage,
        readZone: readZone,
        rankFilter: rankFilterAsym,
        morphRect: morphRect,
        canny: canny,
        normalizeDropout: normalizeDropout,
        applyDropout: applyDropout,
        pixelsToGray: pixelsToGray,
        labOf: labOf,
        ellipseOutline: ellipseOutline,
        rectifyFieldBlock: rectifyFieldBlock,
        decodeLinear: decodeLinear,
        builtinFormats: builtinFormats,
        RuleSet: RuleSet,
        pyRegex: pyRegex,
      },
    };
  }
);
