# OMRChecker (UNIC OMR Scanner): every feature and flag

A reference of everything this program does: features, screens, API, tools, every review flag and every setting.

Source: `badalsura/OMRChecker` master at commit `64d8715` (31 Oct 2026). Every `claude/project-thread-*` branch is already merged, so master is the whole program. About 42,000 lines of Python (`src/`) and 14,600 lines of browser JavaScript (`src/api/static/`). The in-app user guide (`docs/user-guide.md`, 2,700 lines) describes every screen in detail; section numbers below point into it ("UG §2").

---

## 1. Program shell, install and access

| Feature | What it does today | Where |
|---|---|---|
| Windows installer | Online installer (downloads app, OCR models, tools zips; offline if zips sit next to it), per-user or all-users | `packaging/installer`, UG §1 |
| Portable zip / single exe | Unzip-and-run folder; one-file exe for quick tries | `packaging/` |
| Windows 7 SP1 support | Python 3.8 build with pinned wheels | `packaging/requirements-win7.txt` |
| Launcher window | Start/stop server, port, "other devices can connect", open GUI, remote access controls | `packaging/omr_desktop.py` |
| Desktop app window | The screens in their own window, no browser; engine on 127.0.0.1 inside the process. WebView2 via pywebview: system runtime on Windows 10/11, **Fixed Version runtime 109 on Windows 7/8.1** | `packaging/omr_app.py` |
| Data folder | One `omr_data` folder; installed: `%LOCALAPPDATA%`, portable: next to exe, `--data-dir` | `src/api/storage.py` |
| Command line: server | `--port --host --data-dir --workers --api-key --no-browser --no-window --remote --log-level --selftest --version` | UG §1 |
| Command line: headless bulk | `--bulk --template --out --workers --config --evaluation --bubble-model --icr-model --save-images --resume --sheet-timeout`; writes `results.csv` + `results.jsonl` as it goes | `src/batch.py`, UG §1 |
| Self-test | Reads test sheets, decodes test barcodes, starts workers, reports which engines loaded | `--selftest` |
| Remote access | Cloudflare Tunnel (quick tunnel, own hostname, or own tunnel); `cloudflared` bundled | `src/api/tunnel.py`, UG §1 |
| Branding | Change program name, icon, logo | `src/branding.py`, `packaging/apply_branding.py` |
| Sign-in and accounts | Off until first account; first = admin; registration modes approval/open/closed; roles admin, reviewer; PBKDF2 passwords, 30-day sessions, lockout after 10 failures | `src/api/accounts.py`, UG §1 |
| Per-user folders | Non-admins may scan only folders an admin allows | `accounts.py` |
| API key | Shared secret in `X-API-Key` for programs | `settings.py` |
| Licensing | RSA-PSS signed licence file (`license.omrlic`), machine fingerprint, offline request code, features, seats, edition, expiry, anti clock-rollback; online revocation built but dormant | `src/licensing/`, `docs/licensing*.md` |
| Vendor licence tools | License Issuer exe, local licence-admin web GUI, revocation list | `tools/license_admin`, `tools/license_tool.py`, `packaging/license_issuer.py` |
| Help | In-app searchable user guide (`/help`), API docs (OpenAPI) | `src/api/static/help.*` |

## 2. Reading engine

### 2.1 Input
| Feature | Details |
|---|---|
| Images | JPG, PNG, TIFF etc. via OpenCV |
| PDF | Page selection (`pdf_page`: number, range, list), DPI auto or 72-600 |
| Live camera | Browser camera page with sheet guide, light warning, auto capture, exam names |
| In-browser engine | Dependency-free JS engine with parity tests vs Python (`web/omr-browser`) |
| Watched folder | A job keeps picking up new files from a folder (interval, recursive) |
| Scanner drivers | None (files only) |

### 2.2 Image preparation
| Feature | Options |
|---|---|
| Colour dropout | Page-wide or per zone: grey, red, green, blue, max, or a chosen colour with tolerance and strength |
| Dropout fallback | If more than `blankShare` of questions come out blank: re-read in grey, re-read and flag, or flag only |
| Flatten background | Divide by smooth background to remove shadows (on by default) |
| Rotate / flip / straighten | Fixed rotation 0/90/180/270, mirror, small skew angle per template |
| Levels, Gaussian blur, median blur | Pre-processors in template JSON |
| Image quality gate | `poor_image` when sharpness < 10, contrast < 40, bubble < 5 px (each can be 0 = off) |

### 2.3 Alignment (registration)
| Method | Notes |
|---|---|
| None | Sheets already straight |
| Timing tracks (+ index points) | Tracks of marks; size tolerance, search radius, min matched marks, max residual (reject), non-rigid (true/false/auto/tracks), detect orientation, early stop, index seed, joint fit with block corners, trim margins, max region residual, max unmatched share |
| Index points only | 4+ points of any size and shape (square, circle, any), required flag per point, typed X/Y/W/H, calibrate on samples |
| Corner markers | Template match of a marker picture at the 4 corners |
| Page edges (CropPage) | Find the paper outline |
| Feature matching (ORB-style) | Against a reference image; min inliers, max scale change |
| ECC | Fine alignment to a reference image |
| Phone page outline | Find the page in phone photos before registration |
| Orientation handling | Tries 4 orientations; runner-up kept for the `orientation_ambiguous` check |
| Track slide repair | Re-pairs a track ±1 mark when shear or index points say it slid |

### 2.4 Block-level corrections (after page alignment)
| Feature | Default |
|---|---|
| Block snap onto printed bubbles (`block_snap_radius`) | Off (changed answers on some real samples) |
| Fit block onto printed border (`rectifyOnBorder`, padding, outer frame, print image) | Off |
| Block perspective from bubble outlines (`blockPerspective`) | Off |
| Verify bubble fit (reject a correction that fits worse) | On |
| Grid-fit registration check (`min_grid_fit`) | Off |

### 2.5 Bubble reading
| Feature | Details |
|---|---|
| Field types | `QTYPE_INT`, `QTYPE_INT_FROM_1`, `QTYPE_MCQ4`, `QTYPE_MCQ5`, RTL variants, or custom `bubbleValues` + direction |
| Threshold | Adaptive (page and strip thresholds, constants GAMMA_LOW, MIN_GAP, MIN_JUMP, CONFIDENT_SURPLUS, JUMP_DELTA) or fixed (threshold + min fill ratio) |
| Confidence | Distance from threshold vs `confidence_margin` |
| Learned bubble model (ONNX) | Role "second opinion" (disagreement → flag) or "decide" |
| Double-mark rules | `keep_darker_gap` (keep darkest), `strict_double_mark` (faint second mark counts) |
| Custom labels (joined fields) | e.g. roll number from digit columns |
| Group options | Per group: placeholder for empty, multi and doubtful columns |
| Empty value | Global and per block |
| Output columns | Order and selection |
| Primary key / duplicates | Columns that identify a sheet; duplicates listed |
| Out-of-page bubbles | Bubble partly off the page → `out_of_page`, always reviewed |

### 2.6 Zones (non-bubble reading)
| Zone | Engines and options |
|---|---|
| Barcode | Engines in order: zxing, builtin, opencv, pyzbar (optional); formats filter; Code 39 checksum / extended; ITF checksum, min length; fallback zone (e.g. OCR of printed digits) with normalise and review option; review reads by fallback decoders |
| QR code | Same engines |
| OCR (printed text) | Tesseract (C API, best/fast data, languages, PSM, multi-PSM retry, user patterns, cleanup pass) or PaddleOCR PP-OCRv5 ONNX (mobile/server, en/ch/devanagari); fallback engine; disagree → review; min char confidence; direction incl. auto; whitelist; pattern |
| ICR (handwriting) | Trained ICR ONNX model per box, PaddleOCR, Tesseract or VLM (PaddleOCR-VL server, auto-start, timeout); character boxes; second reader |
| Imprint | Scanner-printed dot-matrix number; trained ONNX model or Tesseract; char height; match file name |
| Image zone | Save crop (photo, signature), file name pattern, base64 embed, max side |
| Lazy zones | Read only when a check needs them as fallback |
| Match file name | Compare value with the file name after normalising |
| Scan a code (drag) | On the Results screen, drag a box to decode a barcode |

### 2.7 Rules
| Feature | Options |
|---|---|
| Value validation (`validate`) | length, required, allowGaps, allowEmptyEnds, leadingZeros, pattern, allowed, range; onFail review / blank / both / flag |
| Cross-field checks (`checks`) | sources, priority, normalise, onMissing, onConflict (prefer/review/error), review on conflict / fallback / all missing, skip invalid / flagged, absorb source review, output |

### 2.8 Scoring (`evaluation.json`)
| Feature | Details |
|---|---|
| Answer key | From CSV, from an image of a master sheet, or custom in JSON; answer-key file parser in the editor |
| Marking schemes | Correct / incorrect / unmarked marks (fractions allowed), sections with own scheme |
| Options | grade on/off, question ranges, drop questions, bonus for all, multi-marked scoring (default, incorrect, unmarked, weighted), score legend bands with colours, explain scoring |

### 2.9 Speed
| Feature | Details |
|---|---|
| Worker processes | One per core, prefetch, sheet timeout restarts a stuck worker |
| Fast pass ("test mode") | Read at half size; keep only clear-cut sheets, else re-read |
| Benchmark | Speed and accuracy benchmark runner (`src/benchmark`) |

## 3. Screens and workflow (browser GUI)

Tabs: **Scan, Review queue, Compare, Results, Jobs, Templates** (UG §1 "The tabs at a glance").

| Area | Features | UG |
|---|---|---|
| Scan | Pick template, drop images/PDFs, see answers at once; PDF options; live camera; export CSV of the list | §2 |
| Jobs | Bulk jobs from upload or server folder; name; save images (none/review/all); workers; PDF options; watch folder; pause, resume, cancel, delete; rescan errors; resume after crash; throughput; results.csv per job; exports list (pause/resume/cancel/run again) | §2 |
| Review queue | One item at a time or grid; filters (template, job, flag, kind, name, assigned to); highest-risk-first order; keyboard shortcuts; accept many at once; accept all as read; clear queue for a job; review card with crop and choices | §2 |
| Compare | Reconcile our results with another reader's file (OpScan .dat, CSV, fixed width, packed answer columns); column mapping; saved layouts (profiles); multi-reviewer locking; reconciled layer or write back as corrections; reconciled CSV export; audit | §2 |
| Export | Excel (colour flagged/corrected cells), CSV (leading-zero modes, delimiter), PDF (table / page per sheet), SQLite file, any SQL database via SQLAlchemy URL (upsert by scan id); field selection, renames, types (text/int/decimal/date/bool), sheet columns, per-field corrected/confidence/flags columns; preview; save to server folder; export profiles (save, load, download, upload, JSON) | §2 |
| Results | Filter panel (template, job, status, flag, field, file, assigned to); sheet list badges; canvas views (aligned, full colour, original), side by side, borders, overlay; click to correct; edit values; checks panel; change history; undo/redo; verify; duplicates; failed sheets: place page by hand (click corners/points) or type values; regrade dialog (other settings, current template version, JSON overrides); accuracy dialog; path remap; delete | §3 |
| Check changes | Who did what (scanned, reviewed, corrected, regraded, aligned, typed, verified, approved, assigned, sent back); hand out review work evenly or by sheet numbers; move or take back work; approve in bulk; send a sheet back to review; who may verify | §3 |
| Templates | Upload (JSON or `.omr-template.zip`), auto-generate from ~20 samples (+ optional labels CSV/XLSX), list with status draft/ready, edit, layout picture, download package, duplicate, rename, delete; automatic template versions per read | §4 |
| Template editor | Draw/move blocks and zones; page settings; block properties; block borders dialog; alignment method and options; track editor (tracks, index points, calibrate, find mark/track); groups; output columns dialog; zone options; test read; generated draft checks and warnings; JSON view; review thresholds with fill histogram; learned models; PDF input; barcode advanced; fast pass; validation; cross-field checks; scoring dialog; colours panel (dropout preview and suggestion); locked editing | §4, §5 |
| Template generator | Finds bubble grids, timing tracks, corner marks, barcodes, QR, character boxes; names fields; reads printed bubble values; uses labels; verifies itself on the samples; suggests index points | `src/template_gen` |

## 4. API

156 endpoints (FastAPI, OpenAPI at `/docs`), every GUI action included. Generated clients for Python, Java and Go (`clients/`).

| Area | Endpoints | Examples |
|---|---|---|
| Templates | 32 | CRUD, duplicate, rename, package, files, layout.png, reference, read-sheet, validate, score-preview, confirm-warnings, generate, answer-key parse, generator tools (find mark/track, calibrate index, printed boxes, adopt box, test alignment, verify), alignment samples/test/preview, models |
| Scans | 24 | `POST /scans` (read now), get, image, crop, overlay, render, views, corrections, manual values, manual align, regrade, redo, undo, verify, decode, review, activity, audit |
| Compare | 18 | sessions, preview, profiles, claim/release sheet, recompare, rebuild, export |
| Jobs | 13 | create, get, update, delete, start, pause, resume, cancel, add files, rescan errors, stop watching, results.csv |
| Exports and profiles | 13 | create, preview, list, download, pause/resume/cancel/rerun; export profiles |
| Auth and users | 10 | login, logout, register, password, status, users CRUD, settings |
| Results, review, activity, assignments, audit | 21 | results list, facets, neighbours, accuracy, zone image, delete; review list/counts/summary/states/accept-bulk; activity, approve, return, verify; assignments hand out/move/clear/summary; audit |
| Files, tools, OCR, licence, settings | 13 | browse server folders, roots, recent, check; colour tools and dropout preview/suggest; OCR capabilities; licence status/request/activate; path remap |
| UI and misc | 12 | UI pages, help, guide, branding, health, capabilities, browser engine |

## 5. Developer and data tools

| Tool | What it does | Where |
|---|---|---|
| ML training | Bubble classifier and ICR digit model from review labels; imprint model training | `src/ml` |
| Training labels | Every review decision saves crops and labels (`training/labels.jsonl`) | `src/api/review.py` |
| Synthetic sheets | Render sheets with known answers and distortions | `src/synth` |
| Export CLI | Exports without the GUI | `src/export/__main__.py` |
| Template generator CLI | Generate a template from samples | `src/template_gen/__main__.py` |

---

## 6. Every flag

"To review" = sends the item to the review queue by default. "Tickable" flags are controlled by `review_params.review_flags`; **templates made on the screens start with all tickable flags off**, while older templates and API-uploaded ones keep the old default list (marked "old: yes"). Flags not sent to review are still stored and exported.

### 6.1 Bubble fields
| Flag | Raised when (code) | To review |
|---|---|---|
| `multi_marked` | More than one bubble marked in a single-answer field (`core.py summarize_field`) | Tickable, old: yes |
| `empty` | No bubble marked | Tickable, no |
| `ambiguous_threshold` | No clear gap between dark and light bubbles in the strip (local threshold low confidence) | Tickable, old: yes |
| `low_confidence` | Weakest bubble's confidence < `min_confidence` (0.35) | Tickable, old: yes |
| `weak_mark` | Marked bubble with fill < `min_marked_fill_ratio` (0.25) | Tickable, old: yes |
| `possible_missed_mark` | Unmarked bubble with fill > `max_unmarked_fill_ratio` (0.6) | Tickable, old: yes |
| `model_disagrees` | ONNX bubble model disagrees with the threshold read | Tickable, old: yes |
| `kept_darker_mark` | `keep_darker_gap` turned a double mark into one answer | Tickable, old: no |
| `faint_second_mark` | `strict_double_mark` turned a single mark into a double | Tickable, old: no |
| `rectify_failed` | Block border fit failed; page alignment kept for the block | Tickable, old: no |
| `border_slide` | Border fit would move the block ≥ 0.4 pitch; not applied | Tickable, old: yes |
| `out_of_page` | Part of a bubble lies outside the page | Always |

### 6.2 Joined values (roll numbers etc.)
| Flag | Raised when | To review |
|---|---|---|
| `group_issue` | A column of a group is unclear (blank but flagged for something else, or flagged by a ticked flag) | Always |
| `multi_marked` (group) | A column has two marks | When `multi_marked` ticked |

### 6.3 Zones
| Flag | Raised when (code) | To review |
|---|---|---|
| `not_found` | No code or text found (`barcode.py`, `imprint.py`) | Always |
| `multiple_symbols` | More than one code in the zone | Always |
| `low_confidence` | Zone value confidence < `minConfidence` (0.6) (`base.py finalize`) | Always |
| `pattern_mismatch` | Value doesn't match the zone pattern | Always |
| `engine_unavailable` | OCR engine not installed or failed to start | Always |
| `no_icr_model` | ICR zone without a handwriting model | Always |
| `no_imprint_model` | Imprint zone without a trained model (Tesseract used) | Always |
| `read_error` | Reading the zone raised an error | Always |
| `engine_disagree` | Two OCR engines (or VLM vs ICR model) read different text | Always (if `disagree_to_review`) |
| `low_char_confidence` | A character below `minCharConfidence` (off by default) | Always |
| `unread_box` | An inked ICR box gave no character | Always |
| `icr_alignment` | Line read gave a different count than inked boxes; boxes read singly | Always |
| `file_name_mismatch` | Value differs from the file name (`matchFileName`) | Always |
| `decoded_by_fallback` | A backup barcode engine read the code | Only if `review_fallback_decodes` |
| `not_read` | Lazy zone not needed | Never |
| `empty` (zone) | Nothing read | Not by itself |

### 6.4 Rules and checks
| Flag | Raised when | To review |
|---|---|---|
| `validation_failed` | A `validate` rule failed (length, range, pattern, gaps…) | Per `onFail` (default review) |
| `cross_check_failed` | Sources of a check disagree | Always (`onConflict` review/error, or `reviewOnConflict`) |
| `fallback_used` | First source missing, later one used | Only if `reviewOnFallback` / `onMissing: review` |
| `all_sources_missing` | No source gave a value | Unless `reviewOnAllMissing: false` |

### 6.5 Whole sheet
| Flag | Raised when (code) | To review |
|---|---|---|
| `orientation_ambiguous` | Another orientation fits almost as well (`TimingMarkAlignment.ambiguity_review`) | Always |
| `registration_suspect` | Fit slid one mark along a track scores about as well, or grid fit < `min_grid_fit` (off) | Always |
| `alignment_residual` | A page region's mean residual > `maxRegionResidual` | Always (only if set) |
| `alignment_unmatched` | Unmatched marks + points > `maxUnmatchedShare` (new templates: 15%) | Always (only if set) |
| `index_point_missing` | A required index point not found (points are required by default) | Always |
| `poor_image` | Sharpness, contrast or bubble size under the minimums | Always |
| `too_few_marks` | Fewer marked bubbles than `min_marked_bubbles` (off) | Always (only if set) |
| `dropout_blank` | Most questions blank after colour dropout, action "flag" | Always |
| `dropout_fallback` | Re-read in grey and flagged ("grey_and_flag") | Always |
| `sent_back` | An admin sent the sheet back; this value had been changed | Always |

### 6.6 Sheets that fail completely (status `error`, no fields to review)
| Reason (log message) | Where |
|---|---|
| Timing mark registration failed: matched N/M marks (< `minMatchedMarks`) | `TimingMarkAlignment.apply_filter` |
| Index point registration failed: fewer than 4 index points found | same |
| Registration rejected: residual > `maxResidual` (default 3 px) | same |
| Corner markers not found, or markers don't form a plausible rectangle | `CropOnMarkers.py` |
| ECC did not converge / correlation < `minCorrelation` (0.5) | `EccAlignment.py` |
| No features / too few inliers | `FeatureBasedAlignment.py` |
| Image could not be read; sheet timeout (300 s) | `pipeline.py`, worker |

Today these are rescued by hand on the Results screen (click the corners or index points, or type values).

### 6.7 Sheet statuses and review states
- Sheet status: `ok`, `needs_review`, `error`. Export review status adds `verified`, `reviewed`, `auto`.
- Review item kinds and order (highest risk first): `sheet`, `check`, `custom_label`, `zone`, `field`.
- Review item state: `pending` → `done`.

## 7. Settings catalogue (config.json, per template)

| Section | Keys |
|---|---|
| `dimensions` | display and processing width/height |
| `threshold_params` | GAMMA_LOW, MIN_GAP, MIN_JUMP, CONFIDENT_SURPLUS, JUMP_DELTA, PAGE_TYPE_FOR_THRESHOLD, mode, fixed_threshold, fixed_min_fill_ratio, flatten_background |
| `alignment_params` | auto_align, match_col, max_steps, stride, thickness, block_snap_radius, rectify_on_border, rectify_search_px, rectify_print_image, block_perspective, page_outline, verify_bubble_fit |
| `review_params` | min_grid_fit, min_sharpness, min_contrast, min_bubble_px, confidence_margin, min_confidence, min_marked_fill_ratio, max_unmarked_fill_ratio, min_marked_bubbles, keep_darker_gap, strict_double_mark, review_flags |
| `ml_params` | bubble_model_path, bubble_model_role, icr_model_path |
| `pdf_params` | pdf_dpi, pdf_page |
| `barcode_params` | engines, pyzbar, review_fallback_decodes |
| `ocr_params` | default_engine, fallback_engine, paddle_det_model, paddle_rec_model, paddle_lang, paddle_model_dir, tessdata, langs, multi_psm, cleanup_pass, disagree_to_review, user_patterns, min_char_confidence, icr_second_reader, icr_engine, icr_vlm_model, icr_vlm_url, icr_vlm_autostart, icr_vlm_timeout |
| `speed_params` | fast_pass, fast_scale, fast_clear_empty, fast_clear_marked |
| `outputs` | show_image_level, save_image_level, save_detections, filter_out_multimarked_files |

Server settings (environment): `OMR_DATA_DIR`, `OMR_API_KEY`, `OMR_WORKERS`, `OMR_MAX_UPLOAD_MB` (100), `OMR_SYNC_MAX_FILES` (20), `OMR_ALLOWED_DIRS`, `OMR_MP_START`, `OMR_RESUME_JOBS`, `OMR_SHEET_TIMEOUT_S` (300), `OMR_CORS_ORIGINS`, `OMR_PATH_REMAP`, `OMR_PDF_SHEET_LIMIT` (2000), `OMR_EXPORT_SQL`; licence paths `OMR_LICENSE_*`.

Template keys (`template.json`): bubbleDimensions, pageDimensions, preProcessors, fieldBlocks (origin, bubblesGap, labelsGap, fieldLabels, fieldType or bubbleValues+direction, bubbleDimensions, emptyValue, rectifyOnBorder, borderPadding, outerBorderPadding, blockPerspective), customLabels, groupOptions, outputColumns, primaryKey, emptyValue, alignment, colorDropout, colorDropoutFallback, zones, validate, checks, editorLocked.
