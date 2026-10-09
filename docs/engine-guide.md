# OMR engine guide

This guide covers the production engine built on top of OMRChecker: confidence
and review, sheet registration, barcode/QR/OCR/ICR zones, the library and batch
APIs, the REST service and GUI, automatic template generation, model training,
benchmarking, the in-browser engine, client libraries and Windows packaging.

The classic CLI (`python main.py -i <dir>`) still works as before. It now also
writes `Manual/NeedsReview.csv`.

## How "100% accuracy" is reached

No reader is right on every sheet. Erasures, half-filled bubbles, crossed-out
answers and damaged sheets will always exist. The engine is built so that:

1. every value it reports is either **confidently right** or **sent to review**;
2. the review rate stays low enough that people can clear the queue quickly.

Every field and zone gets a `confidence` in [0, 1], `flags` and `needs_review`.
A sheet's `status` is `ok`, `needs_review` or `error`. Measure both numbers
with the benchmark (`accuracy on auto-accepted values` and `review rate`). Then
tune `review_params` or train a model until auto-accepted accuracy is 100% on
your labelled data.

### Field flags

| flag | meaning | sent to review by default |
|---|---|---|
| `multi_marked` | more than one bubble marked in a single-answer field | yes |
| `empty` | nothing marked | no (blank answers are normal) |
| `ambiguous_threshold` | the strip has no clear dark/light gap and can't use the page-wide threshold | yes |
| `low_confidence` | the weakest bubble sits close to the decision threshold | yes |
| `weak_mark` | a bubble read as marked has little ink inside its circle (partial fill, tick, misregistration) | yes |
| `possible_missed_mark` | an unmarked bubble has a lot of ink inside its circle | yes |
| `model_disagrees` | the learned classifier and the threshold reader disagree | yes |

Zones add `not_found`, `multiple_symbols`, `pattern_mismatch`,
`engine_unavailable`, `no_icr_model` and `read_error`, which go to review, and
`decoded_by_fallback` and `not_read`, which don't (see
[Barcode engines](#barcode-engines) and [Cross-field checks](#cross-field-checks)).
Template rules add `validation_failed`, `cross_check_failed`, `fallback_used`
and `all_sources_missing`.

Tune these in `config.json`:

```json
"review_params": {
  "confidence_margin": 20,
  "min_confidence": 0.35,
  "min_marked_fill_ratio": 0.25,
  "max_unmarked_fill_ratio": 0.6,
  "review_flags": ["multi_marked", "ambiguous_threshold", "low_confidence",
                   "weak_mark", "possible_missed_mark", "model_disagrees"]
}
```

## Designing sheets for machine reading

The engine works with existing sheets, but these choices make the biggest
difference to accuracy and review rate:

- **Timing tracks.** Print a row of solid rectangles down one side and along
  the top or bottom, with one mark per row band. Make the layout asymmetric
  (for example left plus top) so upside-down sheets are detected.
- **Corner markers** as a fallback for scanners that crop edges.
- **Bubbles** at least 3.5 mm in diameter, with light outlines and light
  letters inside. Dark printed letters reduce the contrast between filled and
  empty bubbles.
- **Barcodes.** Code 128 needs a narrow bar of at least 0.25 mm and a quiet
  zone of 10 narrow bars. Scan at 200–300 DPI. A QR code in one corner can
  also carry the template id.
- **Handwriting.** Use one box per character (`characterBoxes`) with
  light-coloured box lines.

## Templates: new options

`template.json` additions:

```json
{
  "preProcessors": [
    {
      "name": "TimingMarkAlignment",
      "options": {
        "tracks": {
          "left": {"marks": [[50, 200], [50, 255], [50, 310]]},
          "top":  {"marks": [[200, 50], [280, 50], [360, 50]]}
        },
        "markDimensions": [24, 12],
        "maxResidual": 3.0,
        "nonRigid": false,
        "detectOrientation": true
      }
    },
    {"name": "EccAlignment", "options": {"reference": "blank.png", "motion": "affine"}}
  ],
  "zones": {
    "sheet_id":  {"type": "barcode", "origin": [700, 110], "dimensions": [460, 120],
                  "options": {"formats": ["Code128"], "pattern": "SHEET-\\d{6}"}},
    "qr":        {"type": "qrcode",  "origin": [560, 90],  "dimensions": [130, 130]},
    "exam_code": {"type": "ocr",     "origin": [140, 120], "dimensions": [380, 60]},
    "candidate": {"type": "icr",     "origin": [140, 230], "dimensions": [360, 60],
                  "options": {"characterBoxes": 6, "whitelist": "0123456789"}}
  }
}
```

**TimingMarkAlignment.** Mark centres are given in template (page) pixels. The
preprocessor works on the original image resolution, tries all four
orientations, fits a RANSAC homography to the matched marks and rejects the
sheet if fewer than `minMatchedMarks` marks match or the mean residual exceeds
`maxResidual`. It then warps straight into template coordinates.
`nonRigid: true` adds a thin-plate-spline correction for curled paper and
phone lens distortion; `nonRigid: "tracks"` instead corrects each row and
column from the timing marks themselves (a vertical track measures row
offsets, a horizontal one column offsets, interpolated between opposite tracks
and never extrapolated past their ends). `indexSeed: true` adds the index
points' own fit as a starting guess for the track search. `earlyStop` (on by default; `false` turns it off) stops trying
orientations once one fits cleanly; 0 and 180 degrees are still both tried (the second with only its 3 unshifted guesses)
unless the index points already decided.

**EccAlignment.** Dense refinement against an image of the blank form. Use it
after a coarse step.

**Zones.** Barcode and QR zones use ZXing-C++ first (see
[Barcode engines](#barcode-engines) for the fallbacks) and accept every
symbology it supports:
- linear codes: Code 128, Code 39 and 93, Codabar, EAN-8 and EAN-13, UPC-A and UPC-E, ITF, DataBar
- 2D codes: PDF417, QR (all versions), Micro QR, rMQR, Data Matrix, Aztec and MaxiCode

Restrict a zone with `formats`. OCR uses Tesseract's LSTM engine, in-process
through `tesserocr` when it is installed, or straight through the libtesseract
library that ships with `tesseract.exe` (set `OMR_LIBTESSERACT` to its path, or
`OMR_TESSERACT_CAPI=0` to turn it off); either takes about 10 ms per zone.
ICR reads boxed characters with a trained crop classifier. Until a model is
configured, ICR results are always sent to review.

Other `config.json` additions:
- `alignment_params.block_snap_radius`: snaps each block in x and y onto its printed bubbles; off by default.
- `ml_params.bubble_model_path` and `ml_params.icr_model_path`: paths to ONNX models.

## Barcode engines

A barcode or QR zone tries these decoders in order and stops at the first one
that reads. Later engines run only when every earlier one read nothing, so
they cost nothing on sheets ZXing reads.

| engine | reads | notes |
|---|---|---|
| `zxing` | every ZXing-C++ symbology | the primary reader, with progressively heavier preprocessing |
| `builtin` | Code 128 (sets A/B/C), Code 39, ITF, EAN-13, EAN-8, UPC-A | pure NumPy, no native library; see below |
| `opencv` | QR Code | `cv2.QRCodeDetectorAruco`, then `cv2.QRCodeDetector` |
| `pyzbar` | ZBar's symbologies | optional: used only when installed **and** switched on |

The zone result records which engine read it in `engine`. A value read by
anything other than `zxing` gets the flag `decoded_by_fallback`. That flag
doesn't send the sheet to review unless you set `review_fallback_decodes`.

```json
"barcode_params": {
  "engines": ["zxing", "builtin", "opencv", "pyzbar"],
  "pyzbar": false,
  "review_fallback_decodes": false
}
```

A zone can override these with `options.engines` (the order) and
`options.pyzbar`. pyzbar isn't in `requirements.txt`; install it with
`pip install pyzbar` (it also needs the ZBar library, `libzbar0` on Linux). The
Windows build bundles it (see [packaging](../packaging/README.md)).

**Built-in decoder** (`src/readers/linear.py`):
- It reads horizontal scanlines across the zone at 12 heights. Tall zones are
  read on their 90° rotation, and every line is also read right to left, so
  upside-down codes work.
- Each line is binarised against its local minimum/maximum envelope, with
  sub-pixel edges. Ink spread is estimated per character, and blurry lines get
  a 1-D sharpening retry.
- It checks quiet zones, module consistency and checksums (Code 128 always;
  EAN/UPC always; Code 39 and ITF on request).
- A value is accepted only when at least 2 scanlines decode the same text.
- Zone options: `code39Checksum` (mod 43), `code39Extended` (`"auto"`, `true`
  or `false`; full ASCII), `itfChecksum` (GS1 mod 10) and `itfMinLength`
  (default 6).

Measured on zxing-generated codes with 1.2–4 px modules, blur, noise and up to
4° rotation, it read 98% of codes, where a single ZXing pass read 85%. It had
no false reads on 2,000 negative images of text, noise, random stripes and
bubble grids. It takes about 2 ms per zone when it reads and about 11 ms
(median) when there is nothing to read.

## Value validation

`validate` checks the shape of any output: a bubble field, a custom label
(such as a multi-column roll number), a zone or a check output.

```json
"validate": {
  "Roll":       {"length": 12, "allowGaps": false, "allowEmptyEnds": false,
                 "leadingZeros": "keep", "onFail": "review"},
  "booklet":    {"pattern": "[A-D]", "onFail": "both"},
  "centre":     {"allowed": ["101", "102", "205"]},
  "marks":      {"range": [0, 100], "onFail": "flag"},
  "answer_book":{"length": [10, 12], "required": true}
}
```

| key | meaning |
|---|---|
| `length` | exact length, or `[min, max]` (either may be `null`) |
| `required` | an empty value fails; otherwise an empty value passes and nothing else is checked |
| `allowGaps` | `false`: an empty column between filled ones fails |
| `allowEmptyEnds` | `false`: empty leading or trailing columns fail |
| `leadingZeros` | `"keep"` (default) or `"forbid"` |
| `pattern` | regular expression the whole value must match |
| `allowed` | list of allowed values |
| `range` | numeric `[min, max]`; the value is compared as a number but stays text |
| `onFail` | `review` (default), `blank`, `both` (blank and review) or `flag` (record only) |

Gap and empty-end checks use the per-column values of a custom label, and the
character boxes of an ICR zone. They don't use the joined string, so a blank
middle digit is caught even when `emptyValue` is `""`. For other values, a
space counts as an empty position.

A failure adds `validation_failed` and the reasons to the field or zone, and is
recorded in `result.validation[name]`:

```json
{"ok": false, "kind": "custom_label", "value": "13456", "reasons": ["gap at position 2"], "action": "review"}
```

- `blank` sets the output to the template's `emptyValue` (a zone's own
  `emptyValue` for zones).
- A failing custom label adds the review item
  `{"kind": "custom_label", "name", "flags", "reasons", "fields"}`.

## Cross-field checks

`checks` combine several reads of one value. Typical uses: a barcode with the
printed digits underneath as a fallback, a QR code that must match a barcode,
or a handwritten number (ICR) compared with its bubbled column.

```json
"checks": [{
  "name": "answer_book",
  "sources": ["barcode2", "sr_no_ocr"],
  "normalize": "digits",
  "priority": ["barcode2", "sr_no_ocr"],
  "onMissing": "fallback",
  "onConflict": "prefer",
  "reviewOnConflict": true,
  "reviewOnFallback": false,
  "output": "answer_book"
}]
```

| key | default | meaning |
|---|---|---|
| `sources` | — | fields, custom labels, zones or other checks' outputs |
| `priority` | `sources` order | which present source wins |
| `normalize` | `"none"` | `none`, `strip`, `digits`, `upper`, `alnum` or `{"regex": "...", "group": 1}`; applied before comparing, and to the output |
| `onMissing` | `fallback` | the first priority source is missing: use the next one (`fallback`), or do that and send the sheet to review (`review`) |
| `onConflict` | `prefer` | sources disagree: keep the priority value (`prefer`), keep it and always review (`review`), or blank the output and review (`error`) |
| `reviewOnConflict` | `true` | with `prefer`, whether a conflict still goes to review |
| `reviewOnFallback` | `false` | review whenever a fallback supplied the value |
| `reviewOnAllMissing` | `true` | review when no source has a value |
| `skipInvalid` | `true` | a source that fails its own `validate` rule counts as missing |
| `skipFlagged` | `false` | a source already flagged for review counts as missing |
| `absorbSourceReview` | `true` | see below |
| `output` | `name` | output column; may be one of the check's own sources, which it then replaces |

- **Output.** The value goes to `responses[output]` and the CSV.
  `result.checks[name]` holds `value`, `chosen_source`, `sources` (raw values),
  `normalized`, `skipped` (sources not used, and why), `flags` and
  `needs_review`. Flags are `fallback_used`, `cross_check_failed` and
  `all_sources_missing`. A check that needs review adds
  `{"kind": "check", "name", "flags"}` to `review`.
- **Order.** Checks can read other checks' outputs. They run in dependency
  order, and a cycle is a template error.
- **Validation.** A check output can have its own `validate` rule. When it fails
  with `blank`, later checks see the output as missing.
- **Source review.** When a check settles a value without needing review, its
  sources' own review flags are cleared: a missing source whose value came from
  another source, or a source that agrees with another one.
  `review_resolved_by` names the check. A source that decided the value alone
  keeps its flags.

**Barcode with printed-digit fallback.** Draw an OCR zone over the printed
number under the barcode and point the barcode zone at it:

```json
"zones": {
  "barcode2":  {"type": "barcode", "origin": [700, 110], "dimensions": [460, 90],
                "options": {"formats": ["Code128"], "fallbackZone": "sr_no_ocr",
                            "fallbackNormalize": "digits"}},
  "sr_no_ocr": {"type": "ocr", "origin": [700, 205], "dimensions": [460, 40],
                "options": {"whitelist": "0123456789"}}
}
```

This creates a check named `barcode2` with sources `[barcode2, sr_no_ocr]`,
`onMissing: fallback` and `onConflict: prefer`. Its output replaces the
`barcode2` column. The OCR zone becomes *lazy*: it is read only when no engine
reads the barcode, so readable sheets pay nothing for it. Until then its result
shows the flag `not_read`. `reviewOnFallback` defaults to `true` here; set
`"reviewOnFallback": false` in the barcode zone's options to accept OCR'd
values without review. Any zone can be made lazy with `"lazy": true`, so that
explicit checks also read it only as a fallback.

**Re-running after corrections.** `src.rules.reapply_rules(result_dict,
template)` re-runs the rules on a stored result. It updates `responses`,
`checks`, `validation`, `review` and `status`.

## Colour sheets: colour dropout

Forms are often printed in a light colour (pink, red, orange, light blue) so
that a scanner can remove the print and keep only the marks. `colorDropout`
in `template.json` says how a colour scan becomes the grey image that is read.
Without the key, sheets are converted to plain grey and decoded in grayscale,
as before.

```json
"colorDropout": {"mode": "red", "strength": 1.0}
"colorDropout": {"mode": "color", "color": "#E8618C", "tolerance": 60}
```

| mode | effect |
|---|---|
| `grey` | plain luminance (the default) |
| `red` | red channel: red, pink and orange print turns light; black, pencil and blue pen stay dark |
| `green`, `blue` | that channel (`blue` removes blue and cyan print) |
| `max` | brightest of B, G, R: every saturated colour turns light, black and pencil stay dark. Blue pen turns light too |
| `color` | colours within `tolerance` (CIE Lab distance, lightness half weighted) of `color` turn white, with a soft falloff up to 1.5 × tolerance |

`strength` (0–1) blends plain grey (0) with the dropout result (1). A string
works as shorthand for a mode, for example `"colorDropout": "red"`.

- **Per zone.** A zone can read a differently processed image:
  `"options": {"colorDropout": "grey"}` (or any setting above). Use it for a
  barcode or text printed in the dropout colour. The extra image is computed
  only when some zone needs it, and it follows the page through every
  registration step.
- **Loading.** Files are decoded in colour only when the template has a
  non-grey dropout (`engine.needs_color`). Otherwise the fast grayscale decode
  is used. The CLI, `src.batch`, API scans and jobs, `OMREngine.scan_path` and
  PDFs all follow this. `OMREngine.scan(image)` accepts a BGR image, which goes
  through the dropout, or a grey image, which is used as-is.
- **Regrading.** `OMREngine(path, template_overrides={"colorDropout": "grey"})`
  reads with other settings without changing the template files.
  `template_overrides` replaces top-level template keys, and `None` removes one.
  `engine.set_color_dropout(spec)` changes a live engine.
- **Pens the same colour as the print** disappear with it. Set
  `review_params.min_marked_bubbles` to flag sheets that have fewer marked
  bubbles than expected. They get a sheet-level review item
  `{"kind": "sheet", "name": "too_few_marks", "flags": ["too_few_marks"]}`.
  The default, 0, turns this off.
- **Cost.** On a 1240 × 1754 page, `red`/`green`/`blue` take about 2 ms,
  `max` about 5 ms and `color` about 20 ms. A colour decode also costs a few
  ms more than a grayscale one.

### Colours panel and tools API

In the template editor, **Colours…** opens a panel. Load a sample sheet to:
- see its main colours with their hex codes and share of the page;
- click the sheet to pick a colour (eyedropper);
- adjust mode, tolerance and strength while comparing before and after;
- write the template's `colorDropout`.

Each print colour comes with a suggested setting that removes it while black,
pencil and blue pens stay dark.

| Endpoint | Input | Returns |
|---|---|---|
| `POST /tools/colors` | multipart `file` (image or PDF), optional `template_id`, `k` | `{colors: [{hex, share, label_guess, suggestion}], paper_share, current}` |
| `POST /tools/dropout-suggest` | JSON `{target: "#RRGGBB", keep: [...]}` | the best settings and how dark each colour ends up |
| `POST /tools/dropout-preview` | `file` and `settings` (colorDropout JSON) | a PNG of what the reader sees |

From Python, use `src.color.extract_palette(image)`,
`suggest_dropout("#E8618C")` and `apply_dropout(image, settings)`.

## Fixed threshold

By default each sheet gets its own page and per-row thresholds. For sheets
with stable printing and scanning, a fixed line is simpler and predictable:

```json
"threshold_params": {"mode": "fixed", "fixed_threshold": 120, "fixed_min_fill_ratio": 0.12}
```

A bubble is marked when at least `fixed_min_fill_ratio` of its interior is
darker than `fixed_threshold`. The adaptive threshold search is skipped, and
`ambiguous_threshold` is never raised. A bubble's confidence is its distance
from the fill line: `|fill - min| / min(min, 1 - min)`, clipped to 1. The
result's `thresholds` gets `"mode": "fixed"`. The editor's Page panel has the
same switch.

## Fitting blocks to a printed border

When a form prints a rectangle around a block of bubbles, the engine can use
it to correct local misplacement that full-page alignment leaves behind, such
as paper curl, a locally stretched print or a bent page in a phone photo.

```json
"fieldBlocks": {
  "MCQ_1": {"...": "...", "rectifyOnBorder": true, "borderPadding": 8}
}
```

Turn it on for every block with `alignment_params.rectify_on_border: true`. A
block's own `rectifyOnBorder` overrides that.

After page alignment, the engine looks for the block's four border lines
within `alignment_params.rectify_search_px` (default 20) of where they are
expected. It fits a quadrilateral and maps the block's bubbles onto it.
`borderPadding` is the gap in pixels between the bubbles and the border, either
a number or `[x, y]`. Without it, the gap is estimated on each sheet. That
corrects shift, rotation and skew, but not a uniform scale error.

The engine keeps the page alignment for the block, and adds the field flag
`rectify_failed`, when any of these happens:
- the border isn't found;
- a corner would move more than the search margin;
- the shape isn't close to a rectangle;
- the printed bubbles fit worse at the corrected positions.

`rectify_failed` is not a review flag by default; add it to
`review_params.review_flags` to send those fields to review. When it is on,
rectification takes precedence over `block_snap_radius` for that block. It
costs about 5 ms per block and nothing when off.

The synthetic renderer can draw block borders (`SheetSpec.block_border`) and
apply local warps (`render_sheet(..., block_warps={"MCQ_1": (dx, dy, angle)})`)
for testing. It can also print the form and the marks in colour (`print_color`,
`ink_color`). The benchmark exposes these as `--print-color`, `--ink-color` and
`--dropout`.

## Library and batch

```python
from src.pipeline import OMREngine
engine = OMREngine("forms/exam/template.json")
result = engine.scan(gray_image, "sheet-001.jpg")
result.status, result.responses, result.review
```

The batch runner uses every core, with one engine per process:

```bash
python -m src.batch --template forms/exam/template.json --input scans/ --out results/ --workers 8
```

It writes `results.csv`, which has a `needs_review` column, and
`results.jsonl`, which has full per-field detail.

On the 4-core development machine, single-threaded reading takes about 65–80 ms
per sheet, measured on synthetic phone-like sheets at roughly 150 DPI:
- registration: about 25 ms
- bubbles: about 15 ms
- zones: about 20 ms

That gives roughly 8–10 sheets per second per core. An 8-core machine should
manage several thousand sheets per minute.

## REST API and GUI

```bash
pip install -r requirements.api.txt
python -m src.api --host 0.0.0.0 --port 8000 --data-dir ./omr_data --workers 8
```

Open `http://host:8000/` for the GUI and `/docs` for the OpenAPI reference.

| Area | What you can do |
|---|---|
| Templates | upload, validate, edit in the visual editor, auto-generate |
| Scans | read synchronously; get crops for review |
| Jobs | run bulk work from uploads or a server folder, with progress, ETA and CSV results |
| Review | work the review queue; corrections rescore the sheet and are saved as training data |
| Results | browse graded sheets with their overlay, toggle bubbles, edit values, verify, regrade, measure accuracy ([below](#results-screen)) |
| Exports | CSV, XLSX, PDF, SQLite or any SQL database, with export profiles ([below](#exports)) |

Set `OMR_API_KEY` to require an `X-API-Key` header. Server folders: once
accounts exist, administrators can browse and scan any folder; every other
user only the folders an administrator allows for that account (**Users >
Server folders**; none means upload only). `OMR_ALLOWED_DIRS` limits everyone,
administrators included. Send `X-User: <name>` to record
who made a correction; the GUI sets it from **User** in the header, and
otherwise corrections are recorded as `local`.

### Accounts and sign-in

Sign-in is off on a new station. **User › Create the administrator account**
turns it on: that first account is the administrator, and from then on the
GUI asks everyone to sign in (`/health` and the static pages stay open).

- **Registration** (User › Manage users): *approval* (default; anyone can
  register and an administrator approves them), *open*, or *closed* (only
  administrators add accounts).
- **Roles**: administrators manage users; everyone signed in can scan,
  review, export and edit templates. The last active administrator can't be
  demoted, disabled or deleted.
- **Audit**: corrections are recorded under the signed-in user name; a
  client's `X-User` header is ignored while someone is signed in.
- **Programs and the SDK** keep using `OMR_API_KEY` (`X-API-Key`), or a token
  from `POST /auth/login` sent as `Authorization: Bearer <token>`.
- **Sessions** last 30 days in an HttpOnly cookie, so images, downloads and
  the live camera page on the same server are signed in too. Disabling an
  account or changing its password signs it out everywhere. Ten wrong
  passwords lock a user name for five minutes.
- Accounts live in `<data dir>/accounts.sqlite3` (PBKDF2-SHA256 password
  hashes). Deleting that file turns sign-in off again.

Endpoints: `GET /auth/status`, `POST /auth/register`, `/auth/login`,
`/auth/logout`, `/auth/password`; administrators: `GET/POST /auth/users`,
`PATCH/DELETE /auth/users/{name}`, `PATCH /auth/settings`.

## Results screen

The **Results** tab is for looking at graded sheets after a job. The **Review**
tab is a queue of single flagged items; the Results tab shows one whole sheet
at a time.

- **Finding sheets.** Use the list on the left. Filter by view, job, template,
  field (a name that was flagged) and flag, or search by file name.
  - Views: all, flagged, reviewed, not reviewed, verified, corrected and errors.
  - The list pages through the SQLite index, so it stays fast with millions of
    sheets.
  - Keys: `j`/`k` (or the arrow keys) move to the next/previous sheet, `n` jumps
    to the next flagged item, `f` fits the image, `o` toggles the overlay, and
    `v` or `Enter` marks the sheet verified.
- **The canvas.** It shows the aligned sheet with the overlay the sheet was
  graded with. Scroll to zoom and drag to pan.
  - Click a bubble to toggle it. The server recomputes the field value
    (multi-marks stay in sheet order), the custom labels, the template's
    rules, the score and the status.
  - Click a zone to edit its value.
- **The sidebar.** It lists every output, flagged first. Each value is editable
  inline:
  - a custom label such as a roll number can be typed whole; it is split over
    its bubble columns, and a space means a blank column;
  - cross-field check outputs and their validation reasons are shown, and so are
    sheet-level items such as `too_few_marks`;
  - pending items have an **Accept** button;
  - the panel also lists all of `result.checks` and the sheet's change history.
- **What a correction keeps.** The machine read survives as `original_value`,
  and the items flagged at read time are kept in `read_review`.
  - Every change is an audit record: who (`X-User` header, or `local`), when,
    the old value and the new value. See `GET /scans/{id}/audit` and `GET /audit`.
  - Verifying a sheet sends its corrected fields to the training data in
    `omr_data/training`.
  - Exports always write the corrected values.
- **Accuracy.** **Accuracy** reports the share of auto-accepted fields
  (not flagged when read) that needed no correction on verified sheets. It is
  broken down per field, so a field that is often wrong without being flagged
  stands out.

Corrections re-run the template's `validate` and `checks` rules. A value a person
set or accepted stays settled while it keeps that value. An edit that breaks a
rule again puts the item back in the review queue. A value typed for a check
output is kept when the rules re-run and when the sheet is regraded.

### How a sheet is re-rendered

No aligned image is stored per sheet by default, which keeps bulk jobs fast.
Instead, every scan records two things:
- the absolute path of its source file (`source_path`);
- the template version it was read with (`template_version`, a hash of
  template.json, config.json and evaluation.json).

Each version is archived under `omr_data/template_versions/`. To show a sheet,
`GET /scans/{id}/render` re-reads the source with an engine for that exact
version. Engines are cached per version, and recent images are cached in memory.
The endpoint returns the overlay JSON, plus the image (`/render/image`, or
inline with `?inline=true`).

- Templates with `colorDropout` are decoded in colour, as in the original read.
- If the source file is gone, a stored `aligned.png` is used (jobs with
  `save_images=all`, or `review` for flagged sheets).
- If neither exists, the endpoint answers 404 and lists every path it tried.
- If a fresh read differs from the stored values, the differences are listed as
  `drift`.

### Path remap (moved input folders)

When the input folder has moved, add a remap rule `old=new`. Rules are tried in
this order:

1. the job's own rules: `PATCH /jobs/{id}` with `{"path_remap": [...]}`;
2. the server's global rules: `PUT /settings/path-remap`, or **Path remap** in
   the Results tab;
3. the `OMR_PATH_REMAP` environment variable, with rules separated by `;` or
   newlines.

```bash
OMR_PATH_REMAP='D:\scans\2024=\\nas\archive\scans\2024;/mnt/old=/mnt/new' python -m src.api
```

Matching is on whole path components. It ignores case and accepts either slash
for Windows-style paths, so `D:\Scans` also matches `d:/scans/day1/a.png`.

### Regrade

`POST /scans/{id}/regrade` re-reads one sheet with changed settings without
editing the template. The GUI has the same thing under **Regrade**.

```json
{
  "template_overrides": {"fieldBlocks": {"MCQ_1": {"bubbleValues": ["D", "C", "B", "A"]}}},
  "config_overrides": {"threshold_params": {"MIN_JUMP": 40}},
  "apply": false,
  "keep_corrections": true
}
```

- `template_overrides` is deep-merged into the sheet's template version and
  validated like the file. For example, `{"colorDropout": "grey"}` swaps the
  dropout, and `null` removes a key.
- `config_overrides` is merged into config.json.
- `apply: false` returns a preview: the new values and a list of the fields
  that would change. The preview image can be viewed side by side in the GUI.
- `apply: true` stores the new read, and the previous read goes to `history`.
- `keep_corrections` re-applies manual corrections on top of the new read.
- `use_current_template: true` regrades with the template as it is now instead
  of the sheet's version.

## Exports

Exports stream results from the index, so memory stays flat, and they always
write corrected values. You can start one from:
- the GUI: **Export…** on a job, or in the Results tab, which uses the current
  filters;
- the API;
- the command line:

```bash
python -m src.export --data-dir omr_data --job JOB_ID --format xlsx --out day1.xlsx --profile profile.json
python -m src.export --jsonl out/results.jsonl --template exam/template.json --format csv
python -m src.export --data-dir omr_data --template-id exam --view verified --format sql \
    --sql-url postgresql+psycopg://user:pw@db/omr
```

| Format | What you get |
|---|---|
| `csv` | UTF-8 with a BOM, so Excel opens it correctly. Leading zeros are kept as text; `csv.leadingZeros: "formula"` writes `="0123"` for Excel. |
| `xlsx` | Write-only openpyxl, so it streams. Values are text cells, so `0123` stays `0123`. Flagged cells are yellow and corrected cells are green. A new sheet starts every `maxRowsPerSheet` rows (Excel's limit is 1,048,576). |
| `pdf` | `mode: "table"` is a results table. `"sheets"` gives one page per sheet: the aligned image with the overlay, plus its values. `"both"` gives both. Per-sheet pages are refused above `pdf.maxSheets` (500), and the server caps that with `OMR_PDF_SHEET_LIMIT` (2000). |
| `sqlite` | A `.sqlite` file, built in (no extra package). |
| `sql` | Any SQLAlchemy URL, for example PostgreSQL with `psycopg`. Rows are upserted in batches on `scan_id`, so re-exporting updates rows instead of duplicating them. `OMR_EXPORT_SQL=0` disables it on a shared server. |

### Export profile

A profile says which columns to write, under which names, in which order and
with which types. Profiles can be saved by name (`PUT /export-profiles/{name}`,
or **Save profile** in the GUI).

```json
{
  "fields": [
    {"field": "RollNumber", "header": "Roll number"},
    {"field": "q1"},
    {"field": "marks", "type": "int"},
    {"field": "dob", "type": "date", "format": "%d%m%Y", "outputFormat": "%Y-%m-%d"},
    {"field": "consent", "type": "bool", "true": ["A"], "false": ["B"]},
    {"field": "notes", "include": false}
  ],
  "includeOtherFields": true,
  "meta": ["file_name", "page", "scan_id", "status", "score"],
  "includeConfidence": false,
  "includeFlags": false,
  "includeReviewStatus": true,
  "includeCorrected": true,
  "includeFieldCorrected": false,
  "allowLossyCast": false,
  "strictCast": false,
  "csv": {"leadingZeros": "none", "delimiter": ","},
  "xlsx": {"maxRowsPerSheet": 1048575, "highlight": true},
  "pdf": {"mode": "table", "maxSheets": 500},
  "sql": {"table": "omr_results", "batchSize": 1000}
}
```

- **Types.** Fields are text unless typed. The types are `text`, `int`,
  `decimal`, `date` and `bool`.
- **Leading zeros.** Casting a value with a leading zero (a roll number `0123`)
  to `int` or `decimal` would lose the zero, so the export is refused unless
  `allowLossyCast` is true.
- **Values that can't be cast.** A value such as `AB` as `int` becomes empty and
  is counted in the export's warnings. Set `strictCast` to fail the export
  instead.
- **Extra columns.** `meta` adds per-sheet columns. You can also add a
  confidence, flags and corrected column per field.
- **Rule outputs.** Cross-field check outputs are ordinary columns.

API:
- `POST /exports` takes `{format, filters, profile | profile_name, sql_url, wait}`
  and runs in the background unless `wait` is set;
- `GET /exports/{id}` gives progress;
- `GET /exports/{id}/download` gives the file;
- `POST /exports/preview` shows the first rows and the warnings before you
  export.

## Automatic template generation

Give it about 20 sheets of the same form. A blank one helps. Add a labels CSV
whose first column is `file_name` and whose other columns are the field labels
with each sheet's true answers:

```bash
python -m src.template_gen --images samples_dir --labels labels.csv --out forms/new_exam
```

Or use **Templates → Auto-generate** in the GUI. The generator:
1. registers the sheets;
2. detects bubble grids, timing tracks, barcodes, QR codes and character boxes;
3. assigns field labels and values by matching fill patterns against your labels;
4. re-reads the input sheets to check itself.

Anything below 99% agreement, and every zone, is listed for you to verify. The
editor highlights each item; fix it and save.

## Training models (when you have data)

```bash
pip install -r requirements.ml.txt

# 1. Build a dataset from labelled scans (or from review corrections in omr_data/training)
python -m src.ml.dataset scans --template T.json --images scans/ --truth truth.csv --out datasets/bubbles

# 2. Train, calibrate and export to ONNX
python -m src.ml.train --data datasets/bubbles --out models/bubble_model.onnx --kind bubble

# 3. Measure before switching it on
python -m src.benchmark --template T.json --images holdout/ --truth holdout.csv --bubble-model models/bubble_model.onnx
```

Then set `ml_params.bubble_model_path` in `config.json`. By default the model
is a second opinion (`ml_params.bubble_model_role: "second_opinion"`): the
threshold reader decides, agreement can raise a bubble's confidence, and
where they disagree the field goes to review (`model_disagrees`). A model
with an extra `erased` (or `crossed`) class sends crossed-out marks to
review too. Set `bubble_model_role: "decide"` only once the benchmark shows
fewer silent errors at the same or lower review rate. ICR models are trained the same
way with `--kind icr`.

## Benchmark

```bash
python -m src.benchmark --template T.json --images dir --truth truth.csv --workers 8 --report report.json
python -m src.benchmark --synthetic 500 --preset phone --zones
```

Reported metrics:
- per-bubble precision and recall
- field and sheet accuracy
- review rate
- accuracy on auto-accepted values
- silent errors
- registration failures
- per-stage p50 and p95 latency and throughput
- the worst errors

## Reading sheets in the browser

`web/omr-browser/omr.js` is a JavaScript port of the reader for web apps that
take phone photos. It reads a photo on the user's device with a preset template
and returns the same result as `ScanResult.to_dict()`. It is plain JavaScript
with no build step, and it runs in Chrome 109+, Safari/iOS 13+, Android Chrome,
Firefox and Node 18+.

```html
<script src="https://your-api/browser/omr.js"></script>
<script>
  OMR.loadTemplate(templateJson)
    .then(engine => engine.scan(fileInput.files[0], { fileId: "photo.jpg" }))
    .then(result => console.log(result.status, result.responses, result.review));
</script>
```

- **Supported:** TimingMarkAlignment (including rotation and `nonRigid`),
  CropPage, CropOnMarkers, Levels, the blur filters and `block_snap_radius`.
  Also `colorDropout` (page and per zone, `templateOverrides`,
  `min_marked_bubbles`), the fixed threshold mode, `rectifyOnBorder`, and
  template `validate` / `checks` (including `fallbackZone` and lazy zones).
- **Matches Python:** on synthetic clean, scanned, phone, colour, bordered,
  fixed-threshold and rules sheets, and on every template in `samples/`, the
  browser reads the same values and review flags. Checks, validation and zone
  results are identical.
- **Speed:** about 120 ms per phone sheet in Node, and 300–450 ms per photo in
  headless Chrome on a slow VM. That includes decoding and drawing. Colour
  dropout adds about 7–20 ms per sheet, plus registering one extra image for
  each per-zone setting.
- **Barcodes:** the built-in decoder (Code 128, Code 39, ITF, EAN/UPC) is part
  of `omr.js`. It is the fallback after zxing-wasm, which is loaded on demand,
  and it is the only decoder when zxing-wasm isn't loaded. The `barcode_params`
  engine chain works as in Python, but the `opencv` and `pyzbar` engines are
  skipped, so QR codes need zxing-wasm.
- **Optional:** the bubble model uses onnxruntime-web, loaded on demand.
- **Not supported:** OCR/ICR zones need a reader you register (for example
  tesseract.js); without one they are flagged for review. FeatureBasedAlignment
  and EccAlignment don't run in the browser.

Send sheets that come back `needs_review` or `error` to `POST /scans` so they
join the review queue.

The API serves the engine and a mobile demo at `/browser/`. To let a web app on
another domain call the API, set `OMR_CORS_ORIGINS` to its origins, separated by
commas. Templates are available from `GET /templates/{id}`, and their marker
images from `GET /templates/{id}/files/{path}`.

See [web/omr-browser/README.md](../web/omr-browser/README.md) for the full API.

## Calling the API from Python, Java and Go

[`clients/`](../clients/README.md) has standard-library-only clients:

| Client | Location | Requires |
|---|---|---|
| Python | `clients/python/omr_client.py` | Python 3.8+ |
| Java | `clients/java`, a Maven project | Java 11+ |
| Go | `clients/go/omrclient` | Go 1.18+ |

All three cover:
- templates, synchronous scans and bulk jobs;
- CSV results and the review queue;
- the Results screen calls: list, render, correct, verify and accuracy;
- exports.

The Python client also covers regrade, audit, path remap and export profiles. `clients/openapi.json` is the spec; regenerate it with
`python clients/export_openapi.py` and use it to generate clients in other
languages.

For millions of files, use a folder job, so the server reads files from its own
disk. Here is the Python client:

```python
from omr_client import OMRClient
client = OMRClient("http://omr-server:8000", api_key="...")
job = client.create_job(template_id="exam", folder="D:/scans/day1", save_images="review")
client.wait_for_job(job["id"])
client.job_results_csv(job["id"], "day1.csv")
```

Measured through the API on a 4-core machine, folder jobs reached:
- **2,964 sheets/min** with `save_images="none"`;
- **1,311 sheets/min** with `"review"`, where images are written for sheets that need review.

## Portable Windows build (Windows 7 and later)

[`packaging/`](../packaging/README.md) builds `OMRChecker.exe` with Python 3.8
and PyInstaller 5.13. Python 3.8 is the last Python that runs on Windows 7.

- **How to build:** run the **Build Windows portable exe** workflow manually, or
  push a `v*` tag. It builds the exe, smoke-tests it on Windows and uploads a
  zip and a single-file exe.
- **Running it:** the exe starts the API and GUI on the local machine, opens
  the browser and keeps its data next to the exe.
- **Headless options:**
  - `OMRChecker.exe --bulk FOLDER --template T.json` runs a batch without the GUI;
  - `--host 0.0.0.0` serves the webapp to other machines;
  - `--selftest` checks the install.
- **Browser needed for the GUI:** Chrome 109 or Firefox ESR 115 on Windows 7.
- **Not yet tested on Windows 7:** CI builds on Windows Server 2022. Run
  `--selftest` once on a real Windows 7 PC.
- **ONNX Runtime on Windows 7:** it may need the updates listed in the
  packaging README. The engine runs without it.
