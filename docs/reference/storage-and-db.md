# OMRChecker (UNIC OMR Scanner): database and storage

A reference of everything this program stores, where and in what shape.

Source: `badalsura/OMRChecker` master at `64d8715` (31 Oct 2026): `src/api/storage.py`, `accounts.py`, `compare.py`, `jobs.py`, `worker.py`, `exports.py`, `results.py`, `manual.py`, `review.py`, `src/licensing/`, `src/export/writers.py`.

## 1. The big picture

- **Files are the source of truth.** Each sheet's full result is a `result.json` file on disk. The SQLite database (`index.sqlite3`) is only an index for fast lists, filters and the review queue, and can be rebuilt from the files at any time (`ScanIndex.rebuild`).
- **Three separate SQLite files**: `index.sqlite3` (sheets, flags, review, audit), `accounts.sqlite3` (users, sessions), `compare.sqlite3` (reconciliation sessions). All use WAL mode where it matters.
- **No relational model of the exam world**: there are no tables for students, schools, exams, centres or organisations. A student exists only as answer values (roll number) inside `result.json`.
- **Answers are not in SQL.** Bubble values, zone values and scores live in JSON only. The only relational answers table is the one the Export feature writes into an external SQLite or SQL database.

## 2. Data folder layout (`omr_data/`)

```
omr_data/
  index.sqlite3                 sheet index, flags, review queue, audit (section 3)
  accounts.sqlite3              users, sessions, registration settings (section 4)
  compare.sqlite3               compare / reconcile sessions (section 5)
  settings.json                 settings edited in the GUI (path remap, recent folders)
  remote_access.json            Cloudflare tunnel settings
  templates/<template_id>/      template.json, config.json, evaluation.json, reference image,
                                marker images, models, alignment samples, answer key files
  template_versions/<template_id>/<hash>/   frozen copy of every template version sheets were read with
  scans/<id[:2]>/<scan_id>/     result.json, aligned.png, marked.jpg, input.<ext>, zones/<crop>.png
                                (sharded by the first 2 hex characters of the id)
  jobs/<job_id>.json            job record (section 6.2)
  jobs/<job_id>.files           list of the job's input files
  jobs/<job_id>/inputs/...      uploaded files of the job
  uploads/                      temporary uploads
  exports/<export_id>.json      export record + the exported file
  export_profiles/              saved export profiles (JSON)
  training/labels.jsonl         one line per review decision (crops + labels)
  training/crops/, training/bubbles/<template_id>/   image crops for model training
license/ (next to the data folder, or %LOCALAPPDATA%\OMRChecker\license)
  license.omrlic, public_key.pem, .license_state
```

## 3. `index.sqlite3` (the sheet index)

### 3.1 `scans`: one row per sheet (per PDF page)
| Column | Type | Meaning |
|---|---|---|
| `id` | TEXT PK | scan id (uuid hex) |
| `template_id` | TEXT | template used |
| `job_id` | TEXT | job it was read in (null for quick scans) |
| `seq` | INTEGER | order within the job |
| `file_name` | TEXT | original file name |
| `status` | TEXT | `ok`, `needs_review`, `error` |
| `score` | REAL | score if the template has an answer key |
| `review_count` | INTEGER | pending review items |
| `reviewed` | INTEGER | 0/1 |
| `has_images` | INTEGER | aligned/marked images saved |
| `error` | TEXT | why the sheet failed |
| `created_at` | REAL | Unix time |
| `flag_count` | INTEGER | added later: number of flags |
| `verified` | INTEGER | added later: a person verified the whole sheet |
| `corrected` | INTEGER | added later: anything changed by a person |
| `template_version` | TEXT | added later: template version hash |
| `primary_key` | TEXT | added later: joined key columns (e.g. roll number) for duplicate detection |
| `approved` | INTEGER | added later: administrator's second check |
| `read_pass` | TEXT | added later: `fast` / `full` (fast-pass mode) |

Indexes: `(status, template_id)`, `(job_id, seq)`, `(template_id, created_at)`, `(job_id, primary_key)`, `(template_id, primary_key)`, `(template_id, job_id, flag_count)`, `(template_id, job_id, verified)`.

Migrations: columns are added with `ALTER TABLE` on start-up when missing (no migration versions).

### 3.2 `review_items`: the review queue
| Column | Meaning |
|---|---|
| `scan_id`, `name` (PK together) | sheet and item name (field, zone, check, group or sheet-level item) |
| `kind` | `sheet`, `check`, `custom_label`, `zone`, `field` (also the review order: highest risk first) |
| `template_id`, `job_id` | copies for filtering |
| `state` | `pending` or `done` |
| `created_at` | REAL |

Indexes: `(state, template_id, job_id)`, `(state, name)`. `WITHOUT ROWID`.

### 3.3 `scan_flags`: every flag as first read
| Column | Meaning |
|---|---|
| `scan_id`, `name`, `flag` (PK together) | one row per flag of an item (flagged items only) |
| `template_id`, `job_id` | for filtering |

Indexes: `(flag, template_id, job_id)`, `(name, template_id, job_id)`. Drives "find all sheets with flag X" and the flag counts.

### 3.4 `corrections`: every manual value change
| Column | Meaning |
|---|---|
| `id` | INTEGER autoincrement |
| `scan_id`, `name`, `kind`, `template_id`, `job_id` | what was changed |
| `old_value`, `new_value`, `original_value` | before, after, as first read |
| `user`, `source`, `at` | who, from where (review, results, compare…), when |

Indexes: `(scan_id, id)`, `(at)`. A copy is also kept in `result.json` → `audit`.

### 3.5 `activity`: who did what
| Column | Meaning |
|---|---|
| `id` | autoincrement |
| `scan_id`, `job_id`, `template_id` | context |
| `action` | e.g. scanned, reviewed, correction, regraded, aligned, typed, verified, unverified, approved, assigned, returned |
| `user`, `detail` (JSON text), `at` | |

Indexes: `(scan_id, id)`, `(at)`, `(user, at)`.

### 3.6 `assignments`: review work handed out
| Column | Meaning |
|---|---|
| `scan_id` PK | one reviewer per sheet |
| `user`, `assigned_by`, `at`, `template_id`, `job_id` | |

Index: `(user, job_id)`.

### 3.7 `field_outcomes`: accuracy readout
| Column | Meaning |
|---|---|
| `scan_id`, `name` (PK) | per field of a **fully verified** sheet |
| `kind`, `template_id`, `job_id` | |
| `flagged`, `corrected` | was it flagged; did a person change it |

Index: `(template_id, job_id, name)`. Gives the "accuracy" dialog: flagged and correct, flagged and wrong, silent errors.

## 4. `accounts.sqlite3`
| Table | Columns |
|---|---|
| `users` | `name` (PK, case-insensitive), `display`, `role` (`admin` / `reviewer`), `active`, `salt`, `hash` (PBKDF2-SHA256, 200,000 rounds), `created_at`, `last_login`, `folders` (JSON list of allowed server folders) |
| `sessions` | `token_hash` (PK, SHA-256 of the cookie token), `name`, `created_at`, `expires_at` (30 days) |
| `settings` | `key`, `value` (registration mode `approval` / `open` / `closed`, etc.) |

Users are referenced everywhere else by **name text**, not by id; there are no foreign keys.

## 5. `compare.sqlite3` (reconcile with another reader's file)
| Table | Purpose and columns |
|---|---|
| `sessions` | one comparison: `id`, `name`, `template_id`, `job_id`, `source_name`, `settings` (JSON), `summary` (JSON), `created_by`, `created_at`, `updated_at` |
| `sheets` | every sheet in scope: `session_id`, `scan_id` (PK together), `ord`, `key`, `file_name`, `row_no`, `mismatches`, `state` (clean, pending, partial, done; or no_record, not_read, replaced, duplicate), `locked_by`, `locked_at`, `done_by`, `done_at` |
| `unmatched` | external rows that matched no sheet: `session_id`, `row_no`, `key`, `reason` |
| `items` | one per mismatch: `session_id`, `scan_id`, `name`, `pos`, `external`, `ours`, `chosen`, `source`, `decided_by`, `decided_at` |
| `audit` | every decision: ids, `name`, `external`, `ours`, `chosen`, `source`, `user`, `action`, `at` |
| `key_cache` | our key value per scan by result-file mtime |
| `profiles` | saved column layouts for a scanner's file: `name`, `template_id`, `settings`, `saved_by`, `saved_at` |

## 6. JSON records on disk

### 6.1 `result.json` (one per sheet; compact JSON)
| Key | Content |
|---|---|
| `scan_id`, `template_id`, `template_version`, `job_id`, `seq`, `page`, `file_name`, `file_id` | identity (`file_id` = `name#pageN` for PDFs) |
| `input_path`, `source_path`, `source_sha256`, `pdf_params` | where the original is, its hash, how it was rendered |
| `status`, `error` | `ok` / `needs_review` / `error` |
| `responses` | final output values by column (after groups, checks and corrections) |
| `fields` | per bubble field: `label`, `value`, `confidence`, `flags`, `needs_review`, `bubbles` [each: `value`, `x`, `y`, `w`, `h`, `mean_intensity`, `fill_ratio`, `marked`, `confidence`, optional `model_disagrees`, `faint_second_mark`, `dropped_lighter_mark`], `original_value` after a correction |
| `zones` | per zone: `name`, `type`, `value`, `confidence`, `flags`, `needs_review`, `box`, `format`, `engine`, `details` (engine reads, second reader, file-name check…) |
| `groups` | per joined field: per-column states |
| `checks`, `validation` | per check: every source's value, chosen source, flags; per validation: result |
| `review`, `read_review` | current review items; the items as first read |
| `review_log` | review decisions per item |
| `score`, `scoring`, `score_error` | score, per-section scores, verdict counts, band |
| `thresholds` | page and strip thresholds used |
| `geometry` | every transform from the original image to the template (for exact re-rendering) |
| `quality` | sharpness, contrast, bubble size |
| `timings_ms` | per stage |
| `read_pass`, `dropout_fallback` | fast-pass outcome; grey re-read facts |
| `key_fields`, `primary_key` | duplicate detection |
| `has_images`, `reviewed`, `verified` (+ who), `corrected`, `approved`, `returned` | workflow state |
| `audit` | every value change (`name`, `kind`, `old`, `new`, `original`, `user`, `source`, `at`) |
| `manual_values`, `manual_entry` | values typed by hand on a failed sheet |
| `manual_alignment` | page placed by hand: `kind`, clicked `points`, `names`, `at`, `by` |
| `regrade` | last regrade: `at`, `by`, template and config overrides, template version, kept corrections |
| `created_at` | Unix time |

Images next to it: `aligned.png`, `marked.jpg` (only if the job saves images: none / review only / all), `input.<ext>` for uploads, `zones/*.png` for image zones (always).

### 6.2 Job (`jobs/<job_id>.json`)
`id`, `template_id`, `source` (upload, folder, or batch: single phone/camera uploads collected by exam name), `state` (uploading, queued, running, paused, watching, completed, failed, cancelled, interrupted), `name`, `created_by`, `options` {`save_images`, `workers`, `prefetch`, `pdf_params`, `watch` {folder, recursive, interval_s, active}}, `path_remap`, `total_files`, `processed_files`, `pages`, `counts` (by status), `errors`, `created_at`, `started_at`, `finished_at`, `throughput_per_s`.

### 6.3 Export (`exports/<export_id>.json`)
`id`, `format` (xlsx, csv, pdf, sqlite, sql), `filters`, `profile`, `state`, `user`, `created_at`, `finished_at`, `rows`, `total`, `warnings`, `error`, `file_name`, `download_url`, `request` (to run again; database URL never stored).

### 6.4 Templates
`templates/<id>/template.json` (layout, alignment, zones, rules), `config.json` (reading settings), `evaluation.json` (answer key and marking), plus assets. Status draft/ready, name and generator report alongside. Every version used for reading is frozen under `template_versions/<id>/<hash>/`. A template travels as `<name>.omr-template.zip`.

### 6.5 Other files
| File | Content |
|---|---|
| `settings.json` | GUI-edited settings: path remap rules, recent folders |
| `remote_access.json` | Cloudflare mode, hostname, token |
| `export_profiles/*.json` | saved export settings (format, folder, fields, renames, types, columns) |
| `training/labels.jsonl` | per review decision: scan, item, crops, true label, predicted, fill ratio |
| `license.omrlic` | `{payload: {license_id, customer, issued_at, expires_at, machine, features, seats, edition, note}, signature, key_id}` (RSA-PSS/SHA-256) |
| `.license_state` | last-seen time (anti clock-rollback) |
| Bulk CLI output | `results.csv` (file, status, score, needs_review, answers) and `results.jsonl` (full results) |
| Vendor side | `revocations.json`, `issued/` copies of issued licences |

## 7. Export targets (results leaving the program)
| Target | Shape |
|---|---|
| SQLite file / SQL database (SQLAlchemy URL, e.g. PostgreSQL) | One table (default `omr_results`): chosen sheet columns + one column per field (typed text/int/decimal/date/bool) + optional `_corrected`, `_confidence`, `_flags` per field; unique on scan id, so re-export updates rows; new columns added with `ALTER TABLE` |
| Excel, CSV, PDF | Same columns as files |
| Reconciled CSV | Compare choices applied on top of our results |
