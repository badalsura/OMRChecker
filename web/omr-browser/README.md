# omr-browser: read OMR sheets in the browser

`omr.js` is a JavaScript port of the Python engine (`src/pipeline.py`, `src/core.py`,
`src/processors/*`). A web app can use it to read a phone photo of a sheet in the
user's browser with a preset template. It returns the same result object as
`OMREngine.scan(...).to_dict()`. Sheets that come back `needs_review` or `error` can
then be uploaded to the Python API for the manual review queue.

- Plain JavaScript (ES2017) with no dependencies and no build step. One file of about 140 KB unminified (about 40 KB gzipped).
- Runs in Chrome 109 (the last version for Windows 7), Safari / iOS 13+, Android Chrome, Firefox, and Node 18+.
- Optional engines are lazy-loaded from URLs you can configure. None of them is bundled:
  - [zxing-wasm](https://github.com/Sec-ant/zxing-wasm), the official ZXing-C++ WebAssembly build, for barcode and QR zones. It reads every format ZXing-C++ supports, including Code 128.
  - [onnxruntime-web](https://onnxruntime.ai/docs/tutorials/web/), for the learned bubble classifier.
  - Any OCR or ICR function you register yourself, e.g. tesseract.js.

| File | What |
|---|---|
| `omr.js` | The engine. A classic script (sets `window.OMR` / `self.OMR`) and a CommonJS module |
| `omr.mjs` | ES-module entry: `import OMR, { loadTemplate } from "./omr.mjs"` |
| `omr.worker.js` | Web Worker host used by `OMR.createWorkerEngine` |
| `demo.html` | Mobile demo: camera or file capture, template presets, overlay, review flags, upload to the server |
| `examples/` | A demo template plus a synthetic phone photo with its ground truth |
| `test/` | Parity tests against the Python engine (`npm test`) |

## Embedding

Script tag:

```html
<script src="/browser/omr.js"></script>
<script>
  OMR.loadTemplate(templateJson).then(function (engine) {
    return engine.scan(fileInput.files[0], { fileId: "sheet-1.jpg" });
  }).then(function (result) {
    console.log(result.status, result.responses, result.review);
  });
</script>
```

ES module:

```js
import OMR from "/browser/omr.mjs";
const engine = await OMR.loadTemplate(await (await fetch("/templates/exam/template.json")).json());
const result = await engine.scan(blob, { fileId: "photo.jpg" });
```

Web Worker (keeps the UI responsive; recommended on phones):

```js
const engine = await OMR.createWorkerEngine(templateJson, {
  workerUrl: "/browser/omr.worker.js",
  barcodes: { scriptUrl: "/vendor/zxing/reader.iife.js", wasmUrl: "/vendor/zxing/zxing_reader.wasm" },
});
const result = await engine.scan(file, { fileId: file.name }); // result.alignedImage is attached
engine.terminate();
```

The page decodes the photo, because canvas access is main-thread only on older Safari.
The gray pixels are then transferred to the worker, which does all the vision work.

## API

| Call | Returns |
|---|---|
| `OMR.loadTemplate(template, options)` | `Promise<Engine>`. `template` is a `template.json` object or its text, or the Python API's `GET /templates/{id}` payload (`{template, config, ...}`). Options: `config` (config.json overrides, merged over the Python defaults), `assets` (`{"omr_marker.jpg": image}`) or `assetsBaseUrl` (where the CropOnMarkers marker image is fetched from), `maxDimension` (default 3000: longest side a photo is decoded to), `evaluation` (optional `responses -> score` function) |
| `engine.scan(source, {fileId, maxDimension})` | `Promise<result>`. `source` can be a `File`/`Blob`, `HTMLImageElement`, `ImageBitmap`, `HTMLCanvasElement`/`OffscreenCanvas`, `HTMLVideoElement`, `ImageData`, or `{width, height, data}` with 1, 3 or 4 channels |
| `engine.setZoneReader(type, fn)` / `OMR.registerZoneReader(type, fn)` | OCR/ICR hook. `fn(crop, zone)` gets a gray `{width, height, data}` crop and returns `{value, confidence, flags?, details?}` (sync or a Promise) |
| `engine.loadBubbleModel({modelUrl, metadataUrl?, metadata?, ort?, ortScriptUrl?, ortWasmPaths?})` | Loads the ONNX bubble classifier (see below) |
| `OMR.enableBarcodes({scriptUrl?, wasmUrl?, module?, wasmBinary?, tryHarder?})` | Loads zxing-wasm. With no arguments it uses the jsDelivr URLs in `OMR.DEFAULT_LIBS` |
| `OMR.createWorkerEngine(template, {workerUrl, omrScriptUrl, extraScripts, barcodes, bubbleModel, config, assets, assetsBaseUrl, maxDimension})` | `Promise<{scan, terminate, info}>` |
| `OMR.checkTemplate(template)` | `{supported, issues[]}` without building an engine |
| `OMR.decodeImage(source, {maxDimension})` | Decodes any source to a gray `{width, height, data}` |
| `OMR.renderResult(canvas, result, {showAll})` | Draws the aligned page with marked bubbles (green), flagged fields (orange) and zones |

The result has the same keys and value shapes as the Python `ScanResult.to_dict()`:
`file_id, status (ok | needs_review | error), responses, fields{label: {label, value,
confidence, flags, needs_review, bubbles[{value, x, y, w, h, mean_intensity, fill_ratio,
marked, confidence, model_marked_prob?, model_disagrees?}]}}, zones{name: {name, type,
value, confidence, flags, needs_review, box, format, details}}, review[{kind, name, flags}],
score, error, thresholds, timings_ms`.

Two extra properties are non-enumerable, so `JSON.stringify(result)` still has
exactly the Python shape:
- `result.alignedImage`: the page warped into template coordinates.
- `result.registration`: method, orientation, matched marks and residual, or the failure reason.

`timings_ms` adds a `decode` entry.

### Sending sheets that need review to the server

```js
if (result.status !== "ok") {
  const form = new FormData();
  form.append("template_id", "exam-2024");
  form.append("files", file, file.name);
  await fetch(apiBase + "/scans", { method: "POST", body: form, headers: { "X-API-Key": key } });
}
```

## Supported template features

| Feature | Browser engine |
|---|---|
| `fieldBlocks` (all `fieldType`s, custom `bubbleValues`, both directions, `emptyValue`), `customLabels`, `outputColumns` | Same as Python |
| Bubble reading: global/local thresholds, Otsu fallback, confidence, fill ratio, every review flag | Ported line by line from `core.py`; the same `threshold_params` / `review_params` |
| `alignment_params.block_snap_radius` | Supported. The bubble-outline mask is an approximation of `cv2.ellipse`, so snapped offsets can differ by about 1 px from Python |
| `TimingMarkAlignment` (orientation 0/90/180/270, residual check, `nonRigid` TPS) | Same algorithm, matching OpenCV's primitives |
| `CropPage` | Same algorithm (truncate, close, Canny, convex hulls, approxPoly, cosine check) |
| `CropOnMarkers` | Same scale search and quadrant matching. NCC runs coarse-to-fine instead of exhaustively (found the same corners on every repository sample). Needs the marker image via `assets` or `assetsBaseUrl` |
| `Levels`, `GaussianBlur`, `MedianBlur` | Supported |
| `FeatureBasedAlignment`, `EccAlignment` | **Not supported**. `loadTemplate` rejects with `unsupported_preprocessor`; process those templates on the server |
| `alignment_params.auto_align` (legacy column shift) | Ignored (it is off by default in Python) |
| Barcode / QR zones | zxing-wasm (`formats` option, all symbologies, `pattern`, `minConfidence`, `emptyValue`). Without it, zones are flagged `engine_unavailable` and the sheet goes to review |
| OCR / ICR zones | Only through a registered reader. Otherwise flagged `engine_unavailable` (ICR also `no_icr_model`), as Python does without Tesseract |
| `evaluation.json` scoring | Not ported. `score` is `null` unless you pass `options.evaluation` |
| PDF input | Not supported (render the page to a canvas first, e.g. with pdf.js) |

### Optional ONNX bubble classifier

The classifier uses the same model and JSON sidecar as `src/ml/classifiers.py`:
`labels` (must include `"marked"`), `input_size`, and an optional `temperature`.

1. Each crop is resized (INTER_AREA) to `input_size`, scaled to [0, 1] and batched as `N×1×H×W`.
2. The logits are divided by the temperature and passed through a softmax.
3. A bubble is marked when `p(marked) >= 0.5`.

When the model and the threshold read disagree, the field gets `model_disagrees`, the same rule as `core.py`.

```js
await engine.loadBubbleModel({ modelUrl: "/models/bubble_model.onnx" }); // fetches bubble_model.json next to it
```

## Accuracy and speed

`npm test` renders synthetic sheets with known answers (`test/make_fixtures.py`, via
`src/synth`), reads them with the Python engine and with `omr.js`, and asserts:
- every field that is not flagged equals the ground truth;
- at least 99.5% value agreement with Python;
- at least 98% `needs_review` agreement with Python.

Measured with 25 sheets per scenario (50 for phone, a third of them rotated 180°):

| Scenario | Fields | Value agreement | needs_review agreement | Unflagged errors | ms/sheet (Node 22, 2.1 GHz Xeon) |
|---|---|---|---|---|---|
| clean (no registration) | 1150 | 100% | 100% | 0 | 14 |
| scan (timing marks) | 1150 | 100% | 100% | 0 | 136 |
| phone (timing marks, 180° flips) | 2300 | 100% | 100% | 0 | 130 |
| CropPage | 1150 | 100% | 100% | 0 | 80 |
| barcode + QR zones (zxing-wasm) | 1150 + 50 zones | 100% (zones equal) | 100% | 0 | 132 |

With `OMR_PARITY_SAMPLES=1`, the photos and scans in `samples/` are also compared.
That covers CropPage, CropOnMarkers, custom labels and per-template configs, and all
20 sample templates agree on 100% of fields.

On the demo photo in headless Chrome, a whole scan takes 300–450 ms, including JPEG
decoding and drawing the overlay. That was measured on a shared VM, so treat it as an
upper bound. The engine itself takes about 130 ms per phone sheet in Node.

Why the two engines agree so closely: the port reproduces OpenCV's primitives, and
was checked pixel by pixel against `cv2`:
- `INTER_AREA` and `INTER_LINEAR` resize, `adaptiveThreshold`, `GaussianBlur`, `Canny`
- the filled `cv2.ellipse` mask used for fill ratios
- `warpPerspective` (OpenCV ≥ 4.11 float bilinear)
- min-max `normalize`

On top of that, the homography is the same least-squares fit on the same matched marks.

Speed tips for phones:
- Use the worker engine.
- Keep `maxDimension` around 2–2.5× the template's longest side.
- Prefer timing-mark templates: CropOnMarkers costs 2–4× more.

## Running the tests

```bash
cd web/omr-browser
npm install            # dev-only: zxing-wasm for the barcode parity test
npm test               # renders fixtures with ../../ Python (needs cv2) into a temp dir
OMR_PARITY_N=25 npm test
OMR_FIXTURES_DIR=/tmp/fx OMR_PARITY_SAMPLES=1 npm test   # reuse fixtures, include samples/
```

`python -m pytest src/tests/test_browser_engine.py` runs the same checks from the
Python test suite. It is skipped when `node` is not installed.

## Self-hosting the optional libraries

The defaults load from jsDelivr (`OMR.DEFAULT_LIBS`). For offline or intranet use,
copy the files to your own server and pass their URLs:

```bash
npm pack zxing-wasm@3.1.4 && tar xf zxing-wasm-3.1.4.tgz
cp package/dist/iife/reader/index.js   /srv/static/vendor/zxing/reader.iife.js
cp package/dist/reader/zxing_reader.wasm /srv/static/vendor/zxing/
npm pack onnxruntime-web@1.20.1 && tar xf onnxruntime-web-1.20.1.tgz
cp package/dist/ort.min.js package/dist/*.wasm package/dist/*.mjs /srv/static/vendor/ort/
```

```js
OMR.enableBarcodes({ scriptUrl: "/vendor/zxing/reader.iife.js", wasmUrl: "/vendor/zxing/zxing_reader.wasm" });
engine.loadBubbleModel({ modelUrl: "/models/bubble_model.onnx", ortScriptUrl: "/vendor/ort/ort.min.js", ortWasmPaths: "/vendor/ort/" });
```

Serve `.wasm` as `application/wasm`. The demo's "Engine libraries" section and its
`?zxingScript=…&zxingWasm=…` query parameters do the same.

## Limitations compared with the Python engine

- No `FeatureBasedAlignment` / `EccAlignment`, no PDF input, and no `evaluation.json` scoring.
- OCR/ICR needs a reader you supply. Without one those zones always go to review.
- Photos are decoded by the browser (EXIF orientation applied) and capped at `maxDimension`. Python reads the full-resolution file, so on very large photos the two engines can see slightly different pixels.
- Colour photos are converted with OpenCV's BT.601 weights. JPEG decoders differ slightly between browsers and libjpeg, so on real photos expect agreement within the review flags rather than bit-exact means.
- `block_snap_radius` uses an approximate outline mask (see the table above).
