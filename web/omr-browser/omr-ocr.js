/*!
 * omr-ocr.js - optional OCR readers for omr.js (opt-in; nothing here is bundled)
 *
 * The browser engine reads OCR / ICR zones only through a reader the page
 * registers (OMR.registerZoneReader). This file offers ready-made readers that
 * load their engine from a CDN at run time, and only when the page calls them:
 *
 *   OMROCR.tesseractReader({ lang: "eng" })        tesseract.js (same engine and models as the server)
 *   OMROCR.paddleReader({ modelUrl, dictUrl })     PaddleOCR PP-OCRv5 recognition on onnxruntime-web
 *                                                   (host the ONNX model yourself; see packaging/ocr_models.md)
 *   OMROCR.withDirection(reader)                   zone option "direction" (rot90cw / rot90ccw / rot180 / auto)
 *   OMROCR.withFallback(first, second)             second engine on an empty / low-confidence / invalid read;
 *                                                   different reads -> flag engine_disagree (review)
 *
 *   OMR.registerZoneReader("ocr", OMROCR.withDirection(OMROCR.tesseractReader()));
 *
 * Mirrors src/readers/text_reader.py. Plain ES2017, works as a classic script
 * (window.OMROCR), in a Web Worker (importScripts) and in Node (require).
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.OMROCR = api;
})(
  typeof globalThis !== "undefined" ? globalThis : typeof self !== "undefined" ? self : typeof window !== "undefined" ? window : undefined,
  function (root) {
    "use strict";

    var LIBS = {
      tesseractScriptUrl: "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js",
      ortScriptUrl: "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/ort.min.js",
    };
    var DIRECTIONS = ["horizontal", "rot90cw", "rot90ccw", "rot180"];

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
        if (typeof document === "undefined") return reject(new Error("Cannot load scripts here: " + url));
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

    // --- crops: { width, height, data: Uint8Array gray } ------------------------
    // Same turns as cv2.rotate in src/readers/text_reader.py rotate_crop
    function rotateGray(img, direction) {
      var w = img.width, h = img.height, src = img.data, out, x, y;
      if (direction === "rot90cw") {
        out = new Uint8Array(w * h);
        for (y = 0; y < h; y++) for (x = 0; x < w; x++) out[x * h + (h - 1 - y)] = src[y * w + x];
        return { width: h, height: w, data: out };
      }
      if (direction === "rot90ccw") {
        out = new Uint8Array(w * h);
        for (y = 0; y < h; y++) for (x = 0; x < w; x++) out[(w - 1 - x) * h + y] = src[y * w + x];
        return { width: h, height: w, data: out };
      }
      if (direction === "rot180") {
        out = new Uint8Array(w * h);
        for (var i = 0; i < w * h; i++) out[w * h - 1 - i] = src[i];
        return { width: w, height: h, data: out };
      }
      return img;
    }

    function zoneOptions(zone) {
      return (zone && zone.options) || {};
    }

    function isValid(text, zone) {
      if (!text) return false;
      var o = zoneOptions(zone);
      if (o.pattern) {
        try {
          if (!new RegExp("^(?:" + o.pattern + ")$").test(text)) return false;
        } catch (e) {
          /* invalid pattern: ignore, as Python does */
        }
      }
      if (o.whitelist) {
        for (var i = 0; i < text.length; i++) if (text[i] !== " " && o.whitelist.indexOf(text[i]) < 0) return false;
      }
      return true;
    }

    function score(out, zone) {
      out = out || {};
      return [isValid(out.value, zone) ? 1 : 0, out.value ? 1 : 0, out.confidence || 0];
    }
    function better(a, b, zone) {
      if (!a) return b;
      if (!b) return a;
      var ka = score(a, zone), kb = score(b, zone);
      for (var i = 0; i < 3; i++) if (kb[i] !== ka[i]) return kb[i] > ka[i] ? b : a;
      return a;
    }

    // Wrap a reader so the zone's "direction" option turns the crop first
    function withDirection(reader) {
      return function (crop, zone) {
        var d = zoneOptions(zone).direction || "horizontal";
        var list = d === "auto" ? DIRECTIONS : [DIRECTIONS.indexOf(d) >= 0 ? d : "horizontal"];
        var best = null, reads = [];
        return list
          .reduce(function (chain, direction) {
            return chain.then(function () {
              return Promise.resolve(reader(rotateGray(crop, direction), zone)).then(function (out) {
                out = Object.assign({ value: "", confidence: 0 }, out || {});
                out.details = Object.assign({}, out.details || {}, { direction: direction });
                reads.push({ direction: direction, value: out.value, confidence: out.confidence });
                best = better(best, out, zone);
              });
            });
          }, Promise.resolve())
          .then(function () {
            if (reads.length > 1) best.details.reads = reads;
            return best;
          });
      };
    }

    function normalise(text) {
      return String(text || "").replace(/\s+/g, "").toUpperCase();
    }

    // Second engine when the first read is empty, below minConfidence or invalid
    function withFallback(first, second, opts) {
      opts = opts || {};
      var disagreeToReview = opts.disagreeToReview !== false;
      return function (crop, zone) {
        var minConf = zoneOptions(zone).minConfidence !== undefined ? zoneOptions(zone).minConfidence : 0.6;
        return Promise.resolve(first(crop, zone)).then(function (a) {
          a = Object.assign({ value: "", confidence: 0 }, a || {});
          if (isValid(a.value, zone) && a.confidence >= minConf) return a;
          return Promise.resolve(second(crop, zone)).then(function (b) {
            b = Object.assign({ value: "", confidence: 0 }, b || {});
            var best = Object.assign({}, better(a, b, zone));
            best.flags = (best.flags || []).slice();
            if (disagreeToReview && a.value && b.value && normalise(a.value) !== normalise(b.value)) best.flags.push("engine_disagree");
            best.details = Object.assign({}, best.details || {}, {
              reads: [
                { engine: a.engine || "first", value: a.value, confidence: a.confidence },
                { engine: b.engine || "second", value: b.value, confidence: b.confidence },
              ],
            });
            return best;
          });
        });
      };
    }

    function grayToCanvas(img) {
      var canvas = typeof OffscreenCanvas !== "undefined" ? new OffscreenCanvas(img.width, img.height) : document.createElement("canvas");
      canvas.width = img.width;
      canvas.height = img.height;
      var ctx = canvas.getContext("2d");
      var rgba = ctx.createImageData(img.width, img.height);
      for (var i = 0; i < img.width * img.height; i++) {
        var v = img.data[i];
        rgba.data[i * 4] = rgba.data[i * 4 + 1] = rgba.data[i * 4 + 2] = v;
        rgba.data[i * 4 + 3] = 255;
      }
      ctx.putImageData(rgba, 0, 0);
      return canvas;
    }

    // --- tesseract.js -------------------------------------------------------------
    // opts: { lang = "eng", scriptUrl, workerOptions (langPath, workerPath, corePath) }
    function tesseractReader(opts) {
      opts = opts || {};
      var workers = {};
      function ready() {
        if (root && root.Tesseract) return Promise.resolve(root.Tesseract);
        return loadScript(opts.scriptUrl || LIBS.tesseractScriptUrl).then(function () {
          if (!root.Tesseract) throw new Error("tesseract.js did not load");
          return root.Tesseract;
        });
      }
      function worker(lang) {
        if (!workers[lang])
          workers[lang] = ready().then(function (T) {
            return T.createWorker(lang, 1, opts.workerOptions || {});
          });
        return workers[lang];
      }
      var reader = function (crop, zone) {
        var o = zoneOptions(zone);
        var lang = o.lang || opts.lang || "eng";
        return worker(lang).then(function (w) {
          return w
            .setParameters({ tessedit_pageseg_mode: String(o.psm || 7), tessedit_char_whitelist: o.whitelist || "" })
            .then(function () {
              return w.recognize(grayToCanvas(crop));
            })
            .then(function (res) {
              var data = (res && res.data) || {};
              var text = String(data.text || "").replace(/\s+/g, " ").trim();
              var chars = (data.symbols || []).map(function (s) {
                return Math.round(s.confidence) / 100;
              });
              return { value: text, confidence: (data.confidence || 0) / 100, engine: "tesseract.js", details: { engine: "tesseract.js", char_confidences: chars } };
            });
        });
      };
      reader.terminate = function () {
        return Promise.all(
          Object.keys(workers).map(function (k) {
            return workers[k].then(function (w) {
              return w.terminate();
            });
          })
        );
      };
      return reader;
    }

    // --- PaddleOCR recognition on onnxruntime-web ----------------------------------
    // Best-path CTC: argmax, merge repeats, drop the blank (index 0). As paddle_ocr.py
    function ctcDecode(probs, steps, classes, characters, allowed) {
      var text = [], conf = [], prev = -1;
      for (var t = 0; t < steps; t++) {
        var best = 0, bestP = -Infinity;
        for (var c = 0; c < classes; c++) {
          if (allowed && c > 0 && !allowed[characters[c]]) continue;
          var p = probs[t * classes + c];
          if (p > bestP) {
            bestP = p;
            best = c;
          }
        }
        if (best !== prev && best !== 0 && best < characters.length) {
          text.push(characters[best]);
          conf.push(bestP);
        }
        prev = best;
      }
      while (text.length && text[0] === " ") text.shift(), conf.shift();
      while (text.length && text[text.length - 1] === " ") text.pop(), conf.pop();
      return { text: text.join(""), confidences: conf };
    }

    // Gray crop -> (1, 3, 48, W) float32 in [-1, 1], bilinear resize
    function recTensor(img, height) {
      height = height || 48;
      var width = Math.min(Math.max(Math.ceil((height * img.width) / Math.max(img.height, 1)), 8), 3200);
      var plane = width * height, out = new Float32Array(3 * plane);
      var sx = img.width / width, sy = img.height / height;
      for (var y = 0; y < height; y++) {
        var fy = Math.min(Math.max((y + 0.5) * sy - 0.5, 0), img.height - 1), y0 = Math.floor(fy), y1 = Math.min(y0 + 1, img.height - 1), wy = fy - y0;
        for (var x = 0; x < width; x++) {
          var fx = Math.min(Math.max((x + 0.5) * sx - 0.5, 0), img.width - 1), x0 = Math.floor(fx), x1 = Math.min(x0 + 1, img.width - 1), wx = fx - x0;
          var d = img.data, w = img.width;
          var v = (d[y0 * w + x0] * (1 - wx) + d[y0 * w + x1] * wx) * (1 - wy) + (d[y1 * w + x0] * (1 - wx) + d[y1 * w + x1] * wx) * wy;
          var n = (v / 255 - 0.5) / 0.5;
          out[y * width + x] = out[plane + y * width + x] = out[2 * plane + y * width + x] = n;
        }
      }
      return { data: out, dims: [1, 3, height, width] };
    }

    // opts: { modelUrl, dictUrl | dictionary (array), ort, ortScriptUrl }
    function paddleReader(opts) {
      opts = opts || {};
      var state = null;
      function ready() {
        if (state) return state;
        var ortReady = opts.ort
          ? Promise.resolve(opts.ort)
          : root && root.ort
          ? Promise.resolve(root.ort)
          : loadScript(opts.ortScriptUrl || LIBS.ortScriptUrl).then(function () {
              if (!root.ort) throw new Error("onnxruntime-web did not load");
              return root.ort;
            });
        var dictReady = opts.dictionary
          ? Promise.resolve(opts.dictionary)
          : fetch(opts.dictUrl).then(function (r) {
              if (!r.ok) throw new Error("Failed to load the PaddleOCR dictionary");
              return r.text().then(function (t) {
                return t.split(/\r?\n/).filter(function (c) {
                  return c !== "";
                });
              });
            });
        state = Promise.all([ortReady, dictReady]).then(function (parts) {
          var ort = parts[0];
          return ort.InferenceSession.create(opts.modelUrl, { executionProviders: ["wasm"] }).then(function (session) {
            return { ort: ort, session: session, characters: ["<blank>"].concat(parts[1], [" "]) };
          });
        });
        return state;
      }
      return function (crop, zone) {
        var o = zoneOptions(zone);
        return ready().then(function (s) {
          var t = recTensor(crop);
          var feeds = {};
          feeds[s.session.inputNames[0]] = new s.ort.Tensor("float32", t.data, t.dims);
          return s.session.run(feeds).then(function (out) {
            var y = out[s.session.outputNames[0]];
            var allowed = null;
            if (o.whitelist) {
              allowed = {};
              for (var i = 0; i < o.whitelist.length; i++) allowed[o.whitelist[i]] = true;
            }
            var r = ctcDecode(y.data, y.dims[1], y.dims[2], s.characters, allowed);
            var mean = r.confidences.length ? r.confidences.reduce(function (a, b) {
              return a + b;
            }, 0) / r.confidences.length : 0;
            return { value: r.text, confidence: mean, engine: "paddle", details: { engine: "paddle", char_confidences: r.confidences } };
          });
        });
      };
    }

    return {
      LIBS: LIBS,
      DIRECTIONS: DIRECTIONS,
      rotateGray: rotateGray,
      isValid: isValid,
      withDirection: withDirection,
      withFallback: withFallback,
      tesseractReader: tesseractReader,
      paddleReader: paddleReader,
      ctcDecode: ctcDecode,
      recTensor: recTensor,
    };
  }
);
