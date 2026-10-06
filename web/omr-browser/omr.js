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
      threshold_params: { GAMMA_LOW: 0.7, MIN_GAP: 30, MIN_JUMP: 25, CONFIDENT_SURPLUS: 5, JUMP_DELTA: 30, PAGE_TYPE_FOR_THRESHOLD: "white" },
      alignment_params: { auto_align: false, match_col: 5, max_steps: 20, stride: 1, thickness: 3, block_snap_radius: 0 },
      review_params: {
        confidence_margin: 20,
        min_confidence: 0.35,
        min_marked_fill_ratio: 0.25,
        max_unmarked_fill_ratio: 0.6,
        review_flags: ["multi_marked", "ambiguous_threshold", "low_confidence", "weak_mark", "possible_missed_mark", "model_disagrees"],
      },
      ml_params: { bubble_model_path: null, icr_model_path: null },
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
    var ZONE_REVIEW_FLAGS = ["not_found", "multiple_symbols", "low_confidence", "pattern_mismatch", "engine_unavailable", "no_icr_model", "read_error"];
    var STATUS_OK = "ok", STATUS_NEEDS_REVIEW = "needs_review", STATUS_ERROR = "error";
    var REGISTRATION_ERROR = "Sheet registration failed (page, markers or timing marks not found)";
    // TimingMarkAlignment
    var TM_DEFAULT_SIZE_TOLERANCE = 0.5, TM_DEFAULT_MIN_MATCHED = 8, TM_DEFAULT_MAX_RESIDUAL = 3.0, TM_MIN_PAGE_AREA_FRACTION = 0.3, TPS_GRID_STEP = 16;
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
        out[i] = (rgba[j] * 4899 + rgba[j + 1] * 9617 + rgba[j + 2] * 1868 + 8192) >> 14;
      }
      return makeImage(width, height, out);
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
    function line2(mask, w, h, p1x, p1y, p2x, p2y) {
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
      fillConvexPoly(mask, w, h, v);
      return mask;
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
          bubbles.push({ x: roundHalfEven(pt[0]), y: roundHalfEven(pt[1]), label: label, value: String(value) });
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
        zones.push({ name: name, type: z.type, origin: [x, y], dimensions: [w, h], options: options, emptyValue: options.emptyValue !== undefined ? options.emptyValue : "" });
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
      var outputColumns = parseFields("Output Columns", t.outputColumns || []);
      if (!outputColumns.length) outputColumns = nonCustom.concat(Object.keys(customLabels)).sort(naturalCompare);
      return {
        json: json,
        pageDimensions: page.slice(),
        bubbleDimensions: t.bubbleDimensions.slice(),
        emptyValue: t.emptyValue,
        preProcessors: t.preProcessors || [],
        fieldBlocks: blocks,
        zones: zones,
        customLabels: customLabels,
        nonCustomLabels: nonCustom,
        outputColumns: outputColumns,
      };
    }

    // ------------------------------------------------------------------------
    // Preprocessors
    // ------------------------------------------------------------------------
    function TimingMarkAlignment(options, page) {
      var self = this;
      this.name = "TimingMarkAlignment";
      this.needsFullResolution = true;
      this.page = page;
      var tracks = options.tracks || {};
      this.trackNames = Object.keys(tracks);
      this.expected = [];
      var spacings = [];
      this.trackNames.forEach(function (n) {
        var marks = tracks[n].marks;
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
      var warped = warpPerspective(image, best.homography, Math.trunc(pageW), Math.trunc(pageH), 255);
      if (this.nonRigid && best.matched >= 6) warped = thinPlateCorrection(warped, best);
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
    TimingMarkAlignment.prototype.fitOrientation = function (corners, candidates, rotation) {
      var self = this;
      var H = this.coarseHomography(corners, rotation);
      if (!H) return null;
      var radius = this.searchRadius * this.pixelsPerUnit(H);
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
        var fit = findHomographyRansac(p.t, p.i, Math.max(2.0, 0.5 * radius), 1234 + a + rotation * 7);
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
      return { rotation: rotation, homography: Hf, matched: finalPairs.length, residual: res, templatePts: fp.t, imagePts: fp.i };
    };

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
    function thinPlateCorrection(warped, fit) {
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
      return remapLinear(warped, mapX, mapY, pw, ph, 255);
    }

    // CropPage: page outline -> warp. Python finds the page with Canny edges on the
    // processing-size image; here the bright page region is segmented instead.
    function CropPage(options) {
      this.name = "CropPage";
      this.needsFullResolution = false;
      this.morphKernel = (options && options.morphKernel) || [10, 10];
    }
    CropPage.prototype.apply = function (image, ctx) {
      var blurred = normalizeMinMax(gaussianBlur(image, 3, 3, 0));
      var quad = findPageQuad(blurred, this.morphKernel);
      if (!quad) {
        ctx.registration = { method: "crop_page", error: "Paper boundary not found" };
        return null;
      }
      ctx.registration = { method: "crop_page", corners: quad };
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
    function findPageQuad(image, morphKernel) {
      var norm = normalizeMinMax(image);
      var lut = new Uint8Array(256);
      for (var v = 0; v < 256; v++) lut[v] = v > 200 ? 200 : v;
      var trunc = normalizeMinMax(applyLut(norm, lut));
      var closed = morphRect(morphRect(trunc, morphKernel[0], morphKernel[1], true), morphKernel[0], morphKernel[1], false);
      var edge = canny(closed, 185, 55);
      var cc = connectedComponents(edge), runs = cc.runs, comps = cc.components;
      if (!comps.length) return null;
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
      var hulls = [];
      comps.forEach(function (comp) {
        if (comp.w * comp.h < MIN_PAGE_AREA_THRESHOLD) return;
        var rows = rowsByComp[comp.label], pts = [];
        Object.keys(rows).forEach(function (k) {
          var yk = Number(k);
          pts.push([rows[k][0], yk]);
          if (rows[k][1] !== rows[k][0]) pts.push([rows[k][1], yk]);
        });
        var hull = convexHull(pts);
        if (hull.length >= 3) hulls.push({ hull: hull, area: polygonArea(hull) });
      });
      hulls.sort(function (a, b) {
        return b.area - a.area;
      });
      for (var hi = 0; hi < hulls.length && hi < 5; hi++) {
        if (hulls[hi].area < MIN_PAGE_AREA_THRESHOLD) continue;
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
      this.k = Math.trunc(options.kSize || 5);
    }
    MedianBlurProcessor.prototype.apply = function (image) {
      return medianBlur(image, this.k);
    };
    function GaussianBlurProcessor(options) {
      this.name = "GaussianBlur";
      this.needsFullResolution = false;
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
    function summarizeField(label, value, bubbles, markedCount, lowConfidence, params) {
      var flags = {};
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
      // ring of each bubble outline (approximates cv2.ellipse thickness 2)
      var pts = [];
      var axW = Math.max(Math.trunc(bw / 2) - 1, 1), axH = Math.max(Math.trunc(bh / 2) - 1, 1);
      var seen = new Uint8Array(blockW * blockH);
      block.fields.forEach(function (f) {
        f.bubbles.forEach(function (b) {
          var cx = Math.trunc(b.x - x0 + bw / 2), cy = Math.trunc(b.y - y0 + bh / 2);
          for (var yy = cy - axH - 2; yy <= cy + axH + 2; yy++)
            for (var xx = cx - axW - 2; xx <= cx + axW + 2; xx++) {
              if (xx < 0 || yy < 0 || xx >= blockW || yy >= blockH) continue;
              var nx = (xx - cx) / axW, ny = (yy - cy) / axH, r = Math.sqrt(nx * nx + ny * ny);
              var dist = Math.abs(r - 1) * Math.min(axW, axH);
              if (dist <= 1.0 && !seen[yy * blockW + xx]) {
                seen[yy * blockW + xx] = 1;
                pts.push(yy * blockW + xx);
              }
            }
        });
      });
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
      if (best - scores[radius * size + radius] < 0.02) return [block.shift, 0];
      return [bx - radius, by - radius];
    }

    function readBubbles(template, image, config, modelProbs) {
      var tp = config.threshold_params, rp = config.review_params;
      var img = image;
      if (img.width !== template.pageDimensions[0] || img.height !== template.pageDimensions[1]) img = resizeLinear(img, template.pageDimensions[0], template.pageDimensions[1]);
      var mm = minMax(img);
      if (mm[1] > mm[0]) img = normalizeMinMax(img);
      var snap = (config.alignment_params && config.alignment_params.block_snap_radius) || 0;
      template.fieldBlocks.forEach(function (block) {
        block.shiftY = 0;
        block.shift = 0;
        if (snap) {
          var s = snapFieldBlock(img, block, snap);
          block.shift = s[0];
          block.shiftY = s[1];
        }
      });
      var allVals = [], strips = [], stds = [];
      template.fieldBlocks.forEach(function (block) {
        var bw = block.bubbleDimensions[0], bh = block.bubbleDimensions[1];
        block.fields.forEach(function (field) {
          var vals = field.bubbles.map(function (b) {
            return boxMean(img, b.x + block.shift, b.y + block.shiftY, bw, bh);
          });
          stds.push(roundTo(stdPop(vals), 2));
          strips.push(vals);
          Array.prototype.push.apply(allVals, vals);
        });
      });
      var globalStdThresh = getGlobalThreshold(stds, tp, 1);
      var globalThr = getGlobalThreshold(allVals, tp, 4);
      var probs = modelProbs ? modelProbs(img, template) : null;
      return Promise.resolve(probs).then(function (modelMarkedProbs) {
        var omrResponse = {}, fieldDetails = {}, thresholdSum = 0, stripNo = 0, boxNo = 0;
        template.fieldBlocks.forEach(function (block) {
          var bw = block.bubbleDimensions[0], bh = block.bubbleDimensions[1];
          block.fields.forEach(function (field) {
            var noOutliers = stds[stripNo] < globalStdThresh;
            var local = getLocalThreshold(strips[stripNo], globalThr, noOutliers, tp);
            var thr = local.threshold;
            thresholdSum += thr;
            var detected = [], details = [];
            field.bubbles.forEach(function (bubble, bi) {
              var mean = strips[stripNo][bi];
              var modelProb = modelMarkedProbs ? modelMarkedProbs[boxNo] : null;
              boxNo++;
              var x = bubble.x + block.shift, y = bubble.y + block.shiftY;
              var marked = thr > mean;
              var fr = fillRatio(img, x, y, bw, bh, thr - rp.confidence_margin);
              var conf = clamp(Math.abs(thr - mean) / rp.confidence_margin, 0, 1);
              var detail = { value: bubble.value, x: x, y: y, w: bw, h: bh, mean_intensity: roundTo(mean, 2), fill_ratio: roundTo(fr, 3), marked: marked, confidence: roundTo(conf, 3) };
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
            fieldDetails[field.label] = summarizeField(field.label, omrResponse[field.label], details, detected.length, local.lowConfidence, rp);
            stripNo++;
          });
        });
        return {
          omrResponse: omrResponse,
          fieldDetails: fieldDetails,
          alignedImage: img,
          thresholds: { global: roundTo(globalThr, 2), global_std: roundTo(globalStdThresh, 2), average_local: stripNo ? roundTo(thresholdSum / stripNo, 2) : 0 },
        };
      });
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
    function readBarcodeZone(zone, aligned) {
      var crop = cropImage(aligned, zone.origin[0] - 10, zone.origin[1] - 10, zone.dimensions[0] + 20, zone.dimensions[1] + 20);
      if (!zxing.lib) return Promise.resolve(zoneResult(zone, "", 0, ["engine_unavailable"]));
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
        if (i >= attempts.length) return Promise.resolve(zoneResult(zone, "", 0, ["not_found"]));
        var img = attempts[i++]();
        if (!img || !img.width || !img.height) return next();
        return Promise.resolve(zxing.lib.readBarcodes(grayToImageData(img), options)).then(function (symbols) {
          symbols = (symbols || []).filter(function (s) {
            return s.isValid !== false || s.text;
          });
          if (!symbols.length) return next();
          var flags = symbols.length > 1 ? ["multiple_symbols"] : [];
          var s0 = symbols[0];
          var r = zoneResult(zone, s0.text, s0.isValid ? 1.0 : 0.0, flags);
          r.format = formatLabel(s0.format);
          r.details = {
            symbols: symbols.map(function (s) {
              return { text: s.text, format: formatLabel(s.format), valid: !!s.isValid };
            }),
            orientation: s0.orientation || 0,
          };
          return r;
        });
      }
      return next();
    }
    function zoneResult(zone, value, confidence, flags) {
      return { name: zone.name, type: zone.type, value: value, confidence: confidence, flags: flags || [], needs_review: false, box: [], format: null, details: {} };
    }
    function finalizeZone(result, zone) {
      var o = zone.options || {};
      if (o.pattern && result.value) {
        var re;
        try {
          re = new RegExp("^(?:" + o.pattern + ")$");
        } catch (e) {
          re = null;
        }
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
        return ZONE_REVIEW_FLAGS.indexOf(f) >= 0;
      });
      if (!result.value) result.value = zone.emptyValue;
      return result;
    }
    function readZone(zone, aligned, readers) {
      var box = [zone.origin[0], zone.origin[1], zone.dimensions[0], zone.dimensions[1]];
      var p;
      try {
        if (zone.type === "barcode" || zone.type === "qrcode") p = readBarcodeZone(zone, aligned);
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
          return finalizeZone(r, zone);
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
                    var x = b.x + block.shift, y = b.y + block.shiftY;
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
    function drawableToGray(drawable, sw, sh, maxDim) {
      var scale = maxDim && Math.max(sw, sh) > maxDim ? maxDim / Math.max(sw, sh) : 1;
      var w = Math.max(1, Math.round(sw * scale)), h = Math.max(1, Math.round(sh * scale));
      var canvas = makeCanvas(w, h), ctx = canvas.getContext("2d");
      if ("imageSmoothingQuality" in ctx) ctx.imageSmoothingQuality = "high";
      ctx.drawImage(drawable, 0, 0, w, h);
      var data = ctx.getImageData(0, 0, w, h);
      return rgbaToGray(data.data, w, h);
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
    // Convert any supported source into a gray image. options.maxDimension caps the
    // longest side (phone photos are downsampled by the browser's own resampler).
    function decodeImage(source, options) {
      options = options || {};
      var maxDim = options.maxDimension === undefined ? 3000 : options.maxDimension;
      return Promise.resolve().then(function () {
        if (!source) throw new Error("No image given");
        if (source.data && source.width && source.height && !(typeof HTMLCanvasElement !== "undefined" && source instanceof HTMLCanvasElement)) {
          var n = source.width * source.height;
          if (source.data.length === n) return makeImage(source.width, source.height, source.data instanceof Uint8Array ? source.data : new Uint8Array(source.data));
          if (source.data.length === n * 4) return rgbaToGray(source.data, source.width, source.height);
          if (source.data.length === n * 3) {
            var out = new Uint8Array(n);
            for (var i = 0, j = 0; i < n; i++, j += 3) out[i] = (source.data[j] * 4899 + source.data[j + 1] * 9617 + source.data[j + 2] * 1868 + 8192) >> 14;
            return makeImage(source.width, source.height, out);
          }
          throw new Error("Unsupported pixel buffer: expected 1, 3 or 4 channels");
        }
        if (typeof Blob !== "undefined" && source instanceof Blob) {
          return blobToDrawable(source).then(function (d) {
            var g = drawableToGray(d, d.width || d.naturalWidth, d.height || d.naturalHeight, maxDim);
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
            return drawableToGray(source, source.naturalWidth, source.naturalHeight, maxDim);
          });
        }
        if (typeof HTMLVideoElement !== "undefined" && source instanceof HTMLVideoElement) return drawableToGray(source, source.videoWidth, source.videoHeight, maxDim);
        if (source.getContext && source.width) {
          if (maxDim && Math.max(source.width, source.height) > maxDim) return drawableToGray(source, source.width, source.height, maxDim);
          var cd = source.getContext("2d").getImageData(0, 0, source.width, source.height);
          return rgbaToGray(cd.data, source.width, source.height);
        }
        if (source.width && source.height && (typeof ImageBitmap !== "undefined" && source instanceof ImageBitmap)) return drawableToGray(source, source.width, source.height, maxDim);
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
    // Registration + preprocessing (ImageInstanceOps.apply_preprocessors)
    Engine.prototype.register = function (gray, ctx) {
      var pre = this.preProcessors, dims = this.config.dimensions;
      var img = gray;
      if (pre.length && !pre[0].needsFullResolution) img = resizeLinear(img, Math.trunc(dims.processing_width), Math.trunc(dims.processing_height));
      for (var i = 0; i < pre.length; i++) {
        img = pre[i].apply(img, ctx);
        if (!img) return null;
      }
      return img;
    };
    Engine.prototype.scan = function (source, opts) {
      var self = this;
      opts = opts || {};
      var fileId = opts.fileId || "image";
      var started = now(), timings = {};
      return decodeImage(source, { maxDimension: opts.maxDimension !== undefined ? opts.maxDimension : this.options.maxDimension })
        .then(function (gray) {
          timings.decode = elapsed(started);
          return self.scanGray(gray, fileId, started, timings);
        })
        .catch(function (error) {
          return makeResult(fileId, STATUS_ERROR, { error: String((error && error.message) || error), timings_ms: timings });
        });
    };
    Engine.prototype.scanGray = function (gray, fileId, started, timings) {
      var self = this, template = this.template;
      started = started || now();
      timings = timings || {};
      var step = now(), ctx = {};
      var aligned = this.register(gray, ctx);
      timings.registration = elapsed(step);
      if (!aligned) {
        timings.total = elapsed(started);
        var r = makeResult(fileId, STATUS_ERROR, { error: REGISTRATION_ERROR, timings_ms: timings });
        hide(r, "registration", ctx.registration || null);
        return Promise.resolve(r);
      }
      step = now();
      var model = this.bubbleModel;
      return readBubbles(template, aligned, this.config, model ? model.predictMarked : null).then(function (detailed) {
        timings.bubbles = elapsed(step);
        var zstep = now();
        return Promise.all(
          template.zones.map(function (z) {
            return readZone(z, detailed.alignedImage, self.zoneReaders);
          })
        ).then(function (zoneList) {
          timings.zones = elapsed(zstep);
          var omr = Object.assign({}, detailed.omrResponse), zones = {};
          zoneList.forEach(function (z) {
            omr[z.name] = z.value;
            zones[z.name] = z;
          });
          var responses = {};
          Object.keys(template.customLabels).forEach(function (label) {
            responses[label] = template.customLabels[label]
              .map(function (k) {
                return omr[k];
              })
              .join("");
          });
          template.nonCustomLabels.forEach(function (label) {
            responses[label] = omr[label];
          });
          var review = [];
          Object.keys(detailed.fieldDetails).forEach(function (name) {
            var d = detailed.fieldDetails[name];
            if (d.needs_review) review.push({ kind: "field", name: name, flags: d.flags });
          });
          zoneList.forEach(function (z) {
            if (z.needs_review) review.push({ kind: "zone", name: z.name, flags: z.flags });
          });
          var score = self.evaluation ? self.evaluation(responses) : null;
          timings.total = elapsed(started);
          var result = makeResult(fileId, review.length ? STATUS_NEEDS_REVIEW : STATUS_OK, {
            responses: responses,
            fields: detailed.fieldDetails,
            zones: zones,
            review: review,
            score: score,
            thresholds: detailed.thresholds,
            timings_ms: timings,
          });
          hide(result, "alignedImage", detailed.alignedImage);
          hide(result, "registration", ctx.registration || null);
          return result;
        });
      });
    };
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
        if ((z.type === "barcode" || z.type === "qrcode") && !zxing.lib) issues.push("Zone '" + n + "' needs OMR.enableBarcodes()");
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
      var initOptions = { config: options.config, maxDimension: options.maxDimension, omrScriptUrl: options.omrScriptUrl, extraScripts: options.extraScripts || [], barcodes: options.barcodes || null, bubbleModel: options.bubbleModel || null };
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
          scan: function (source, opts) {
            opts = opts || {};
            var t0 = now();
            return decodeImage(source, { maxDimension: opts.maxDimension !== undefined ? opts.maxDimension : options.maxDimension }).then(function (gray) {
              var decodeMs = elapsed(t0);
              var buf = gray.data.buffer.byteLength === gray.data.length ? gray.data.buffer : gray.data.slice().buffer;
              return call("scan", { width: gray.width, height: gray.height, data: buf, fileId: opts.fileId || "image", returnImage: opts.returnImage !== false }, [buf]).then(function (res) {
                var result = res.result;
                result.timings_ms.decode = decodeMs;
                if (res.image) hide(result, "alignedImage", makeImage(res.image.width, res.image.height, new Uint8Array(res.image.data)));
                hide(result, "registration", res.registration || null);
                return result;
              });
            });
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
        rankFilter: rankFilterAsym,
        morphRect: morphRect,
        canny: canny,
      },
    };
  }
);
