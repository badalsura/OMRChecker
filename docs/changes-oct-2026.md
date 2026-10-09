# Changes, October 2026

Everything added or changed in the October 2026 work on the engine, the
station GUI, the template editor and the live camera page (PR #5, 43
commits). The settings and API calls named here are the ones to look up in
[engine-guide.md](engine-guide.md); this page says what changed and why.

Everything stays Python 3.8 compatible (the Windows 7 build).

Contents:

- [Reading accuracy](#reading-accuracy)
- [Speed](#speed)
- [Jobs](#jobs)
- [Template editor](#template-editor)
- [Automatic template generation](#automatic-template-generation)
- [Review queue](#review-queue)
- [Results screen](#results-screen)
- [Failed sheets](#failed-sheets)
- [Duplicate sheets (primary key)](#duplicate-sheets-primary-key)
- [Live camera page and camera uploads](#live-camera-page-and-camera-uploads)
- [New settings at a glance](#new-settings-at-a-glance)
- [New and changed API calls](#new-and-changed-api-calls)
- [Fixes](#fixes)
- [How-to notes from the same discussion](#how-to-notes-from-the-same-discussion)

## Reading accuracy

- **Background flattening.** Before bubbles are measured, the page is divided
  by a smooth estimate of the paper, so shadows and uneven light no longer
  shift the threshold (`threshold_params.flatten_background`, on by default).
  The browser engine (`omr.js`) does the same, so both read alike.
- **Orientation and slide ambiguity.** Timing-mark registration keeps the
  runner-up orientation. A sheet is flagged `orientation_ambiguous` when two
  orientations tie and the index points don't decide. It is flagged
  `registration_suspect` when a fit shifted one mark along a track scores
  about as well as the chosen one. A degenerate guess is skipped instead of
  failing the sheet.
- **Grid-fit check (optional).** Compares where the template puts the bubbles
  with the printed bubble outlines and flags `registration_suspect` below
  `review_params.min_grid_fit`. It is off by default because it flagged some
  good real samples.
- **Capped block snap.** `block_snap_radius: -1` means automatic (0.3 × the
  block's bubble pitch). A block is never moved 0.4 pitch or more, which would
  be the neighbouring bubble.
- **Printed borders.** A block can be fitted to a printed border around it,
  with the gap measured and the outer frame detected. A border may move the
  bubbles by at most 0.4 pitch; a bigger move is flagged `border_slide` for
  review instead.
- **Fit blocks without a box to their bubbles** (`block_perspective`, existing
  setting; the block option is now labelled "Fit to bubble outlines"). Tested
  on the Bundelkhand set: turning it on cut `weak_mark` flags from 35 to 7 and
  review items from 38 to 27, with no answer changes.
- **Image quality gate.** Very blurred, low-contrast or tiny-bubble photos are
  flagged `poor_image` (`review_params.min_sharpness`, `min_contrast`,
  `min_bubble_px`; set any to 0 to turn it off). The thresholds only catch
  truly unreadable photos.
- **Steeply turned photos.** Timing marks are judged by their own shape
  (second moments), so photos taken at a steep angle still register.
- **Index-point seed and track-grid correction (optional).**
  `timingMarks.indexSeed` uses the index points for the first guess;
  `nonRigid: "tracks"` corrects along the timing-track grid. Both are stored
  and replayed exactly.
- **Learned bubble model is a second opinion.** By default the threshold read
  decides; when the model disagrees, or sees an erased mark, the bubble goes
  to review (`ml_params.bubble_model_role`, `"decide"` to let the model
  decide). The model reads the unflattened page, like its training crops.
- **Original file fingerprint.** The SHA-256 of each original is stored, and
  re-renders and the Original view refuse a file whose contents changed.

## Speed

- **Faster reading, identical results.** Mark shapes are checked per blob,
  bubble fills are measured in batches, the page is copied less often and
  settings lookups are faster. Checked on 300 sheets × 4 templates: every
  answer, fill, geometry, quality value, review item and stored image is
  unchanged.
- **Faster orientation search.** `timingMarks.earlyStop` is on by default.
  Once one orientation fits cleanly, the others only try their unshifted
  guesses. On a two-track sheet this cut orientation search from about 282 ms
  to about 100 ms.
- **Worker CPU setup.** Each worker process uses one OpenCV/BLAS/ONNX thread.
  On Windows 10/11 background workers are not power-throttled. The default
  worker count is the number of logical processors. Tesseract can run
  in-process through ctypes, falling back to the executable per language.
- **Read-ahead.** Bulk jobs read the next files into memory ahead of the
  workers, so disk and decode overlap (per-job "Read ahead", 64 files by
  default, 0 turns it off).
- **Speed benchmark.**
  `python -m src.benchmark.speed --template T.json --images DIR --workers 1 8 16`
  prints sheets per second and the time per stage.

## Jobs

- **Pause and Resume.** Sheets being read finish, then the job stops. Resume
  reads the remaining sheets. A paused job can be cancelled, including one
  still winding down after Pause.
- **Cancel confirmation.** Cancel asks first, like Delete.
- **No stored images.** Jobs that keep no images still show review crops. The
  aligned page is rebuilt from the original file with the stored geometry.
- **Camera upload jobs.** Sheets sent from the camera page with an exam name
  are collected into one job per exam. See
  [Live camera page and camera uploads](#live-camera-page-and-camera-uploads).
- **Python 3.8 fix.** Jobs were marked failed after reading every sheet,
  because `shutdown(cancel_futures=True)` needs Python 3.9. This is fixed.

## Template editor

- **Tidier side panel.**
  - Checkboxes sit beside their labels, and help text shows on hover.
  - Alignment, Grouped fields, Cross-field checks and Options are collapsible.
  - Block settings fold into "Position and spacing" and "Alignment" (the
    count shows how many alignment settings are set). Labels are shorter;
    the help is unchanged.
  - Shortcuts moved to a **?** button in the toolbar.
  - The duplicate "Detect sheets fed upside down" switch was removed.
- **Bubble display.** Bubble values are drawn inside the bubbles, with field
  names and output positions. Digit blocks with an unusual value order (for
  example 1–9, 0 when the rest are 0–9) are drawn amber.
- **Index points.** X/Y and size can be typed in. Index points can be
  calibrated on sample sheets. Each point's **required** checkbox decides
  whether a sheet missing it goes to review.
- **Printed border tool.** Pick the printed border for a block; the gap is
  measured and an outer frame detected.
- **Verification list.** The "Needs verification" list keeps Verified buttons.
- **Columns… dialog** (top bar). It lists every output column in export
  order: groups, single bubble fields, zones and check outputs. Each row
  shows what it holds, for example "group · 12 col · digits".
  - **↑ Up / ↓ Down** move the ticked columns together and save that as a
    custom export order.
  - **Combine…** joins the ticked columns into one grouped field. Ticked
    groups merge into the new one, which takes the first column's place.
    Digits and letters can be combined; the joined value is text.
  - **Split** turns a group back into separate columns.
  - Zones and check outputs can be moved but not combined; groups are built
    from bubble columns.
- **Turn the sheet** (`alignment.rotate`, 0/90/180/270). This turns every
  sheet before it is read. It is recorded as a geometry step, so stored reads
  replay it. It is offered per sheet in Regrade too.

## Automatic template generation

- Never dead-ends: the first sheet always gives at least an empty draft.
- Suggests index points when there are no timing tracks. A single straight
  track gets four index points, because a single track cannot register a
  sheet alone.
- Reads bubble values from the print when no labels are given. Weak blocks
  without labels are held back as suggestions instead of being added.
- Layout warnings, and samples that fail to read leave the template "not
  ready".

## Review queue

- **Loading and order.** It loads when the tab opens, highest-risk items
  first.
- **Filters.** Dropdowns for **Job** and **Flag**. The flag filter shows only
  items read with that flag; it applies to the list, the counts and bulk
  accept.
- **Grid view.** A grid of one field across many sheets, or grouped **by
  flag** (all `weak_mark` items together, for example).
- **Whole-sheet items.** Sheet-level items, such as a missing index point,
  show the whole sheet with every field marked. Found index points are green
  and missing ones red, and the reason names the missing point.
- **Accept all.** Runs in the background in chunks of 500, with progress and
  a Stop button.

## Results screen

- **Show menu** has a **Not flagged** option.
- **Verified sheets are no longer flagged.** Verifying a sheet removes its
  flagged status:
  - It leaves Show › Flagged and appears under Not flagged.
  - Its ⚑ badge is hidden; the ✓ tooltip still says how many fields were
    flagged when read.
  - Its fields are no longer marked flagged.

  The same goes for a single field or group a person accepted or corrected.
  The read flags stay listed and count in the accuracy statistics. Undoing
  the verification brings the sheet back.
- **Accept and stepping on.**
  - An accepted change no longer carries over to the next sheet. A late
    server reply only updates the sheet it belongs to.
  - A slower load of an earlier sheet no longer replaces the one on screen.
  - The selection follows to the next sheet only when that field needs a look
    there too.
- **Group Accept** also accepts the group's flagged columns.
- **Regrade with controls.** The Regrade dialog has controls for:
  - turning the sheet
  - colour removal
  - bubble threshold (adaptive or a fixed level)
  - mark sensitivity
  - evening out shadows
  - finding the page outline
  - fitting blocks to printed borders
  - per-block perspective

  Each defaults to the template's setting. The raw JSON overrides are under
  "Advanced" and win where both set a value.
- **Drag to scan barcodes and QR codes.** Barcode and QR fields have a
  **Scan** button. Drag a box around the code on the sheet (aligned or
  original view, Esc cancels). The original scan is decoded at full
  resolution inside the box, and the value is saved as a correction.
- **Registration summary and page outline.** The sheet details show how the
  sheet was registered. The Original view draws the page outline. The
  alignment line no longer shows "residual NaN px".

## Failed sheets

A sheet that failed to register can be fixed on the Results screen in two
ways:

- **Align by clicking.** Click the page corners or the index points on the
  original, then re-read.
- **Type the values.** Enter them directly; the form is prefilled from what
  was read.

## Duplicate sheets (primary key)

The template's `primaryKey` lists the output columns that identify a sheet,
for example a roll number. The key is stored with each scan. Results has a
**Duplicates** view, and duplicate sheets link to each other.

## Live camera page and camera uploads

This is the browser page `/browser/demo.html`; with `?source=server` it uses
this station.

- **Server connection.**
  - The API base URL defaults to the station's own address.
  - Templates are picked from a dropdown (↻ reloads the list).
  - The Scan tab has a **Live camera** button that opens the page with the
    selected template and the API key.
- **Overlay.** The template is drawn over the video as an alignment guide.
  Before a sheet is found, the page outline, tracks and index points sit
  centred in the frame. Once the sheet is found, the overlay follows it. The
  overlay is light: page outline and index points, which turn green or red.
- **Ready rule.** Ready needs the page fitted, every index point seen, a
  sharp image and a steady hand. The preview updates every 0.2 s, then the
  frame is captured.
- **Take photo** uses the same in-page camera and overlay, and falls back to
  the phone's camera app without camera access. The camera closes after the
  shot. A photo that fails alignment shows **Retake**.
- **Exam / olympiad grouping.**
  - The Exam / olympiad field sends `batch` with each upload, so all sheets
    of one exam land in one job. Review, Results, CSV export and cleanup then
    work per exam through the Job filters.
  - **Source label for the exam** sets the job's label; the default is
    "camera / single uploads".
  - **Sheet name** renames the uploaded photo; empty keeps the photo's name.

## New settings at a glance

| Setting | Where | Default | What it does |
|---|---|---|---|
| `threshold_params.flatten_background` | config | `true` | Even out shadows before reading bubbles |
| `alignment_params.block_snap_radius` | config | `0` | `-1` = automatic, 0.3 × pitch |
| `review_params.min_grid_fit` | config | `0` (off) | Flag sheets whose printed bubbles don't line up |
| `review_params.min_sharpness`, `min_contrast`, `min_bubble_px` | config | 10, 40, 5 | Image quality gate (`poor_image`) |
| `review_flags` gains `border_slide` | config | on | Review a block a printed border would move too far |
| `ml_params.bubble_model_role` | config | `second_opinion` | Learned model as second opinion or decider |
| `alignment.rotate` | template | 0 | Turn every sheet 90/180/270° before reading |
| `timingMarks.earlyStop` | template | on | Faster orientation search |
| `timingMarks.indexSeed` | template | off | Use index points for the first registration guess |
| `timingMarks.nonRigid: "tracks"` | template | — | Correct along the timing-track grid |
| `primaryKey` | template | — | Columns that identify a sheet (duplicates) |

## New and changed API calls

| Call | What it does |
|---|---|
| `POST /jobs/{id}/pause`, `POST /jobs/{id}/resume` | Pause and resume a job |
| `PATCH /jobs/{id}` | Also takes `label` |
| `POST /scans` | Takes `batch` (exam name, collects sheets in one job) and `batch_label`; returns `job_id` |
| `GET/POST /scans/{id}/manual-align` | Align a failed sheet by clicked points |
| `POST /scans/{id}/manual-values` | Type the values of a failed sheet |
| `POST /scans/{id}/decode` | Decode a barcode/QR inside a box drawn on the sheet |
| `POST /scans/{id}/regrade` | Template overrides may include `alignment.rotate` |
| `GET /review`, `/review/counts`, bulk accept | Take a `flag` filter |
| `GET /review/summary` | Adds `by_flag` and `by_job` |
| `GET /results?view=` | Adds `unflagged`; `flagged` leaves out verified sheets |
| `POST /templates/{id}/generator/printed-boxes`, `/calibrate-index`, `/verify` | Template generator helpers |

## Windows exe: bundled OCR, handwriting and remote access

- Every build bundles Tesseract ("best" English, Hindi), PaddleOCR PP-OCRv5
  (detection and the main recogniser in mobile and server size, English and
  Devanagari recognisers) and `cloudflared`; the build stops if one is missing
  (`packaging/prepare_bundle.py`).
- Template editor, OCR and ICR zones: "PaddleOCR model" (mobile / server) and
  "PaddleOCR recogniser" (English / main / Devanagari); models the server lacks
  are marked "not installed".
- ICR zones without a trained ICR model are read by PaddleOCR, preferring the
  main recogniser (trained on handwriting too). Boxed zones are read as one line, or box by box
  when the line gives the wrong count; the usual `minConfidence` decides
  review. `ocr_params.icr_engine: "tesseract"` restores the old behaviour
  (Tesseract, always reviewed).
- Launcher window: port, Start / Stop server, Start / Stop remote, Cloudflare
  settings (quick tunnel, tunnel token, or API token + hostname) with Copy
  buttons for both addresses, and Quit. See
  [packaging/README.md](../packaging/README.md#the-launcher-window).

## Fixes

- Jobs: a pause could race a job's start (409 on resume); a queued job is now
  claimed under the lock.
- Jobs on Python 3.8 were marked failed (see [Jobs](#jobs)).
- Generator: blocks that were held back left their custom labels behind, so
  the template failed to load.
- OCR: an in-process Tesseract failure falls back per language.
- API: malformed manual-align points return 422; calibrate-index reports a
  template that fails to load.
- GUI: editor caches and modes reset when another template opens. Manual-align
  clicks no longer leak to other sheets. The index point panel no longer shows
  "null".
- Camera upload file names with spaces are made safe (`Roll 17` becomes
  `Roll_17.png`).

## How-to notes from the same discussion

**Cleaning up after a batch of sheets.**
- Jobs › Delete removes the job's results, review items, flags, stored images
  and database rows.
- The corrections audit log is kept on purpose.
- The SQLite file does not shrink by itself; it reuses the freed space.

**Fastest image format to decode.** Measured on one sheet:
- greyscale JPEG: 4.4 ms
- colour JPEG (the current default): 11.1 ms
- BMP: 1.3 ms, but 2.2 MB per sheet

**Using the camera from a phone (Windows).**
1. Run the station with an API key (`OMR_API_KEY`).
2. Expose it over HTTPS: the launcher's **Start remote** (or `cloudflared tunnel --url http://localhost:<port>`).
3. Open `<tunnel URL>/browser/demo.html?source=server` on the phone. The API
   base URL fills in by itself.

**Reviewing camera uploads.**
- Review queue or Results, Job dropdown → the exam's job. Single uploads with
  no exam name go to the "camera / single uploads" job.
- After updating the code, restart the server: an older running server does
  not have the new review images or views.
