# OCR engines and models in a build

OCR zones are read through one interface (`src/readers/text_reader.py`) with two
engines behind it:

| Engine | Runs on | Models |
| --- | --- | --- |
| Tesseract 5 (LSTM) | `tesserocr` in-process, or `tesseract.exe` via `pytesseract` | `tessdata_best` (accurate, about 15 MB for English) or `tessdata_fast` (about 4 MB) |
| PaddleOCR PP-OCRv5 | the `onnxruntime` the app already ships; no PaddlePaddle | detection (DB) + recognition (CTC), mobile or server size, converted to ONNX |

No model file is committed to git. A build fetches what it bundles with
`packaging/fetch_ocr_models.py`, which downloads from the official sources:

* Tesseract: `github.com/tesseract-ocr/tessdata_best` and `tessdata_fast`.
* PaddleOCR: the official PP-OCRv5 inference models from PaddlePaddle's model
  server (`paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/...`),
  converted to ONNX with `paddle2onnx` on the build machine. The character
  dictionary is taken from each model's `inference.yml`. If you already have
  converted ONNX files, `--paddle-onnx-dir DIR` copies them instead.

## Build choices

```
# Default build: Tesseract "best" English, no PaddleOCR (today's behaviour, better model)
python packaging/fetch_ocr_models.py

# Smaller build: the "fast" English model only
python packaging/fetch_ocr_models.py --tessdata fast

# Hindi as well, both Tesseract sets (the first set listed is the default)
python packaging/fetch_ocr_models.py --tessdata best fast --langs eng hin

# Tesseract first, PaddleOCR mobile as fallback (English + Devanagari)
pip install paddle2onnx            # build machine only
python packaging/fetch_ocr_models.py --paddle mobile --paddle-langs en devanagari --fallback-engine paddle

# PaddleOCR as the default engine, server-size models, Tesseract as fallback
python packaging/fetch_ocr_models.py --paddle server --default-engine paddle --fallback-engine tesseract
```

| Switch | Meaning | `ocr_params` key it sets |
| --- | --- | --- |
| `--tessdata best\|fast [..]` | Tesseract model sets bundled; first is the default | `tessdata` |
| `--langs eng hin ..` | Tesseract languages bundled | `langs` (English stays the default) |
| `--paddle mobile\|server` | Bundle PaddleOCR (omit for none) | `paddle_det_model`, `paddle_rec_model` |
| `--paddle-langs en ch devanagari` | PaddleOCR recognition models; first is the default | `paddle_lang` |
| `--default-engine tesseract\|paddle` | Engine for zones that do not choose one | `default_engine` |
| `--fallback-engine none\|tesseract\|paddle` | Engine tried when a read is empty, low-confidence or invalid | `fallback_engine` |

Server-size recognition exists for the multilingual (`ch`, which also reads
English) model only; for `en` and `devanagari` the mobile model is used.

The script writes (all git-ignored):

```
packaging/tessdata/best/<lang>.traineddata
packaging/tessdata/fast/<lang>.traineddata
packaging/models/paddleocr/*.onnx, *_dict.txt
packaging/ocr_build.json        {"ocr_params": {...build defaults...}}
```

`packaging/omr.spec` bundles these as `tessdata/`, `models/paddleocr/` and
`ocr_build.json` next to the exe. A source checkout finds them in `packaging/`
too, and `OMR_TESSDATA_BEST`, `OMR_TESSDATA_FAST`, `OMR_PADDLE_MODELS` and
`OMR_OCR_BUILD` point at other locations.

## The same switches in config.json

`ocr_build.json` only changes the defaults. Any `config.json` can override them
without a rebuild:

```json
"ocr_params": {
  "default_engine": "tesseract",
  "fallback_engine": "paddle",
  "paddle_det_model": "mobile",
  "paddle_rec_model": "mobile",
  "paddle_lang": "en",
  "tessdata": "best",
  "langs": ["eng"],
  "multi_psm": true,
  "cleanup_pass": true,
  "disagree_to_review": true,
  "user_patterns": true,
  "min_char_confidence": 0,
  "icr_second_reader": "auto"
}
```

Per zone (template.json zone `options`): `engine` (`default`/`tesseract`/`paddle`),
`fallbackEngine`, `lang`, `direction`, `psmRetry`, `userPatterns`,
`minCharConfidence`.

## When an engine is missing

* PaddleOCR models not bundled, or `onnxruntime` fails to load (Windows 7
  without KB2999226 and friends): PaddleOCR reports itself unavailable, logs
  once, and zones use Tesseract alone. The zone details say
  `engine_unavailable: paddle` with the reason.
* `tessdata/best` not bundled: the Tesseract install's own models are used, as
  before.
* No Tesseract at all and no PaddleOCR: OCR zones are flagged
  `engine_unavailable` and go to review, as before.

`GET /ocr/capabilities` shows what a running server has (engines, installed
languages, defaults); the zone editor uses it for its dropdowns.

## In the browser (web/omr-browser)

The browser reader stays OCR-free unless a page opts in. `omr-ocr.js` offers
two optional hooks, both loaded from a CDN at run time only when called:
`tesseractReader()` (tesseract.js) and `paddleReader()` (onnxruntime-web with
PP-OCRv5 ONNX models you host). Nothing is bundled and the core GUI never
needs them.
