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
- [Double marks](#double-marks)
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
- **A track paired one mark off is repaired.** Timing tracks repeat, so on
  a sheet cut short or fed unevenly one track could pair every mark with its
  neighbour while the other paired correctly. The fit then sheared the page,
  every mark still matched with a low residual, and one side of the sheet was
  read a row off (on the "Jhansi Mid Test Fast Run" job: 35 of 6344 sheets,
  20 of them with no registration flag; healthy sheets shear under 0.006,
  these about 0.02). When index points are missing or the fit shears more
  than 0.012, each track is now re-paired up to one mark either way and the
  fit finding the most index points, then without shear, wins. Sheets with
  every index point found and no shear skip this. Also in the browser reader
  (shear only, it does not use index points).
- **Fast pass works with "Bend to fit the marks"** (Exact and Smooth curve),
  and has its own editor panel (Options › Fast pass). On 199 Jhansi sheets
  with each mode, 163 were kept from the half-size read and all 163 matched
  the full read.
- **`nonRigid: "tracks"` lands every mark on its template place.** It also
  corrects side to side: a vertical track's marks drifting left or right are
  followed when there is no horizontal track (and the other way round). The
  template editor's "Bend to fit the marks" is now a choice: Off, Exact
  (`"tracks"`), Smooth curve (`true`) or Curve when marks cover the page
  (`"auto"`).
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
- **Non-rigid alignment.** The spline basis of the remap grid is reused across
  sheets: the same maps, 53 ms down to 15 ms per sheet.
- **Timing-mark blob filter.** Mark shapes are measured for all blobs at once,
  about 2.5× faster with identical results.
- **Fast pass (test mode, off by default).** With
  `speed_params.fast_pass: true` each sheet is first read at half size. The
  result is kept only when nothing needs review, every bubble is clearly empty
  (`fast_clear_empty`, 0.25) or clearly filled (`fast_clear_marked`, 0.6) and
  every zone was read; anything else is read again at full size. On 800 real
  sheets 72% were kept from the fast read, with 0 answer differences and the
  same accuracy; time per sheet fell from 193 to 137 ms on one worker.
  Results say which read was kept in `read_pass` (`fast` or `full`). Only
  timing-mark templates without non-rigid alignment, border fitting, OCR or
  image zones, a bubble model or `auto_align` use it. Python engine only.

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
- **Crashed or hung readers.** A reader process that crashes, or is still on
  one sheet after `OMR_SHEET_TIMEOUT_S` seconds (300; 0 = no limit), no longer
  ends the job. The readers are restarted, the sheets they had are read again
  one at a time, and the one that fails on its own is stored as an error. A
  job that still fails (the readers kept crashing) can be resumed.
- **Time taken.** The Jobs table has a **Time** column (`elapsed_s` in the
  job): the time spent reading, with the start-up of the reader processes
  included and pauses left out; a resumed job adds to it. The live Sheets/s
  counter uses the same clock and updates every second, also while the
  readers are still starting.
- **Rescan errors.** A button on the Jobs tab reads a job's error sheets
  again; sheets someone already reviewed are kept. Works on completed,
  paused, interrupted and failed jobs.
- **No duplicates after a crash.** A sheet read again (resume after a crash,
  rescan) replaces its earlier result instead of adding a second one.
- **`--bulk` / `python -m src.batch`.** `--resume` keeps the sheets already in
  `--out`, then reads the rest and the error sheets. Every result is written
  to disk at once, and a crashed or hung sheet (`--sheet-timeout`, 300 s, with
  2+ workers) becomes an error row instead of ending the run.

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

- **Mirror in Regrade.** Regrade › **Mirror the sheet (after the turn)**:
  left to right or top to bottom (`alignment.flip`). The template editor's
  Alignment panel has **Turn every sheet** and **Mirror every sheet**.
- **Undo and redo on a sheet.** Results toolbar **↶ Undo** / **↷ Redo**
  (Ctrl+Z, Ctrl+Y) take back or redo the open sheet's last change, regrades
  and hand alignments included (last 20 changes per sheet).
- **Image settings in Regrade.** An Image group with Turn, Mirror,
  **Straighten** (`alignment.straighten`, small angle) and **Line the image up
  by** (template's alignment, page edges only, or nothing), with a picture of
  the scan as it will be read.
- **Regrade saves at once.** After **Apply** the sheet's list row and the
  review queue show the new read too (they kept the old status and values
  until reloaded).
- **Who did what.** Every action on a sheet is logged with the person:
  scanned, reviewed (accepted as read), values changed, regraded, aligned or
  typed by hand, verified, verification taken back. The side panel's **Who did
  what** shows it per sheet.
- **Check changes.** A Results toolbar screen listing the sheets people
  changed and nobody verified since, with who did what, the changed settings
  and the changed values; **Verify** per sheet, **Verify ticked** and
  **Verify all on this page** (administrators when sign-in is on).

## Double marks

Two optional `review_params` settings (0 = off), also under **Review
thresholds** in the template editor:

- **`keep_darker_gap`** ("Keep darker mark when ahead by"): when two or more
  bubbles are marked and the darkest is filled this much more than the next,
  only the darkest counts (flag `kept_darker_mark`).
- **`strict_double_mark`** ("Strict double marks from"): when one bubble is
  marked, any other bubble filled at least this much also counts, so the
  question becomes a double mark (flag `faint_second_mark`, plus
  `multi_marked`). A field `keep_darker_gap` settled is left alone.

On 800 real sheets, strict at 0.30 caught 45 missed double marks and also
sent 76 pen dots or ticks to review; keep-darker never matched the answer key,
which writes every double mark as `*`. Both apply to every bubble field, roll
numbers included.

## Name and icon: UNIC OMR Scanner

The program is now called **UNIC OMR Scanner** and has its own icon (browser
tab, app header, sign-in page, launcher window, exe, installer and its
shortcuts). Both live in `branding/` (`branding.json` and `icon.png`); see
"Name and icon (branding)" in `packaging/README.md` to change them.
`GET /branding` returns the name for other front ends. The exe, install
folder and data folder are still called `OMRChecker`, so existing results
stay where they are.

## Failed sheets

A sheet that failed to register can be fixed on the Results screen in two
ways:

- **Align by clicking.** Click the page corners or the index points on the
  original, then re-read.
- **Type the values.** Enter them directly; the form is prefilled from what
  was read.
- **Place the page by hand.** Also offered for any sheet from the Regrade
  dialog ("Place the page by hand…"): the template's timing marks (red), index
  points (blue) and bubbles (grey) are drawn over the original scan. Drag the
  four corner circles to stretch, or the centre cross to move the whole page,
  until they sit on the printed ones; arrow keys nudge by 1 px (Shift: 10),
  "Turn 180°" / "Turn 90°" fix a sheet read the wrong way up. "Regrade with
  this placement" reads the sheet again through it (the same call as
  aligning by corners, so it is stored and replayed the same way).

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
  - With the field empty, sheets go into the job "Camera / phone uploads"
    (one per template). Sheets dropped on the Scan tab go into "Single
    uploads" the same way.
  - **Source label for the exam** sets the job's label; the default is
    "camera / single uploads".
  - **Sheet name** renames the uploaded photo; empty keeps the photo's name.

## Check changes tab and handing out review work

- **Check changes** is now its own tab. Administrators hand out flagged sheets to reviewers and other administrators in equal shares, see each person's progress (assigned, still to review, reviewed, verified), look at what each person changed, take sheets back, verify changed sheets, or **Send back** sheets to review (to a chosen person, with a note). Every hand-out and send-back is logged.
- **Results** has an **Assigned to** filter (Anyone, Me, Nobody). The side panel shows who a sheet is assigned to and a warning when it was sent back.
- With sign-in on, only administrators can verify sheets.
- **Original as background** now shows the sheet after its turn, mirror and straighten, so a sheet regraded with Turn 180° no longer shows upside down. Hand placement works on that turned view.

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
| `alignment.flip` | template | none | Then mirror it: `horizontal` or `vertical` |
| `alignment.straighten` | template | 0 | Then turn it by a small angle (degrees, clockwise +) |
| Alignment method **Index points only** | template editor | — | TimingMarkAlignment with no tracks and 4+ `indexPoints` |
| `timingMarks.earlyStop` | template | on | Faster orientation search |
| `timingMarks.indexSeed` | template | off | Use index points for the first registration guess |
| `timingMarks.nonRigid: "tracks"` | template | — | Correct along the timing-track grid |
| `primaryKey` | template | — | Columns that identify a sheet (duplicates) |
| `review_params.keep_darker_gap` | config | `0` (off) | Keep only the darkest of several marks when it is clearly ahead |
| `review_params.strict_double_mark` | config | `0` (off) | A second bubble filled this much makes a double mark |
| `review_flags` gains `kept_darker_mark`, `faint_second_mark` | config | off | Review the fields those two settings changed |
| `speed_params.fast_pass` (`fast_scale`, `fast_clear_empty`, `fast_clear_marked`) | config | `false` | Half-size first read (test mode) |
| `OMR_SHEET_TIMEOUT_S` | environment | `300` | Skip a sheet still being read after this long |
| `--resume`, `--sheet-timeout` | `--bulk` / `src.batch` | — | Continue a stopped run; skip a hung sheet |

## New and changed API calls

| Call | What it does |
|---|---|
| `POST /jobs/{id}/pause`, `POST /jobs/{id}/resume` | Pause and resume a job; resume also takes failed jobs |
| `POST /jobs/{id}/rescan-errors` | Read a job's error sheets again; returns `rescan_files` |
| `PATCH /jobs/{id}` | Also takes `label` |
| `POST /scans` | Takes `batch` (exam name, collects sheets in one job) and `batch_label`; returns `job_id` |
| `GET/POST /scans/{id}/manual-align` | Align a sheet by clicked points or a hand placement; GET also returns the template's `timing_marks`, `mark_size` and `bubbles` to draw |
| `POST /scans/{id}/manual-values` | Type the values of a failed sheet |
| `POST /scans/{id}/decode` | Decode a barcode/QR inside a box drawn on the sheet |
| `POST /scans/{id}/regrade` | Template overrides may include `alignment.rotate` and `alignment.flip` |
| `POST /scans/{id}/undo`, `POST /scans/{id}/redo` | Undo or redo the sheet's last change; sheet payloads carry `undo: {undo, redo}` |
| `GET /scans/{id}/activity`, `GET /activity` | Who scanned, reviewed, changed, regraded, aligned, typed and verified |
| `GET /activity/sheets`, `POST /activity/verify` | Changed sheets not verified since, and bulk verify |
| `POST /activity/return` | Send sheets back to review, optionally to a person, with a note |
| `POST /assignments`, `POST /assignments/clear`, `GET /assignments/summary` | Hand out flagged sheets to people in equal shares, take them back, and see each person's progress |
| `GET /results?assigned=` | `@me`, a user name, or `__none__` for sheets nobody has |
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

## Online installer for Windows

A small `OMRChecker-setup-<version>.exe` installs OMR Checker like a normal
program (Start menu, optional desktop icon, uninstaller). While installing it
downloads the program, the OCR models and the tools from the GitHub release
and checks each file's SHA-256; on a PC without internet, put the three zips
next to Setup.exe. Data stays in `%LOCALAPPDATA%\OMRChecker\omr_data`, so
upgrades and uninstalls never touch it. Build one by running **Build Windows
portable exe** with `installer_release` set, or by pushing a `v*` tag. See
[packaging/README.md](../packaging/README.md#online-installer).

## Server folders per user

- Administrators browse and scan every folder on the server. Other users see
  and scan only the folders an administrator allowed for their account
  (**Users > Server folders**, typed or picked with Browse); with none they
  can only upload files. Recent folders are filtered the same way.
- `PATCH /auth/users/{name}` takes `folders` (a list of full paths);
  `/fs/roots` reports `per_user`. `OMR_ALLOWED_DIRS` still limits everyone.

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
