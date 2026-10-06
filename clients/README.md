# OMR Engine API clients

Clients for the bulk/REST API (`python -m src.api` or `OMRChecker.exe --host 0.0.0.0`).
All three have the same operations and no third-party dependencies.

| Operation | Python `OMRClient` | Java `OmrClient` | Go `omrclient.Client` |
| --- | --- | --- | --- |
| health / capabilities | `health()`, `capabilities()` | `health()`, `capabilities()` | `Health`, `Capabilities` |
| templates | `list_templates()`, `upload_template(paths)` | `listTemplates()`, `uploadTemplate(name, paths)` | `ListTemplates`, `UploadTemplate` |
| synchronous read (few files) | `scan(template_id, files)` | `scan(...)`, `scanResults(...)` | `Scan` |
| bulk job (uploads and/or server folder) | `create_job(template_id, files=, folder=, workers=, start=)` | `createJob(...)`, `createFolderJob(...)` | `CreateJob(JobOptions{...})` |
| add files / start / cancel | `upload_job_files`, `start_job`, `cancel_job` | `uploadJobFiles`, `startJob`, `cancelJob` | `UploadJobFiles`, `StartJob`, `CancelJob` |
| progress | `job_status`, `wait_for_job(job_id, poll, callback)` | `jobStatus`, `waitForJob(id, poll, onPoll)` | `JobStatus`, `WaitForJob(ctx, id, poll, onPoll)` |
| results | `job_results_csv(job_id, out_path)` | `jobResultsCsv(id, path)` | `JobResultsCSV`, `JobResultsCSVTo` |
| one page | `get_scan(scan_id)` | `getScan(id)` | `GetScan` |
| human review | `review_queue(...)`, `submit_review(scan_id, corrections, accept)` | `reviewQueue(params)`, `submitReview(...)` | `ReviewQueue`, `SubmitReview` |
| results screen (list, render, edit) | `list_results`, `iter_results`, `render`, `render_image`, `overlay`, `correct(scan_id, changes, toggle=)`, `verify`, `regrade`, `audit` | `listResults`, `render`, `correct`, `verify`, `regrade` | `ListResults`, `Render`, `Correct`, `Verify` |
| accuracy / path remap / job settings | `accuracy`, `path_remap`, `set_path_remap`, `update_job` | `accuracy` | `Accuracy` |
| exports (csv, xlsx, pdf, sqlite, sql) | `export(filters, fmt, out_path, profile=)`, `create_export`, `export_status`, `download_export`, `export_profiles`, `save_export_profile` | `createExport`, `exportStatus`, `downloadExport` | `CreateExport`, `WaitForExport`, `DownloadExport` |

Pass the API key (server started with `OMR_API_KEY` / `--api-key`) as the second
constructor argument; it is sent as `X-API-Key`. The Python client also takes
`user=` (sent as `X-User`), which is recorded in the audit trail of corrections.

**Fastest bulk path:** put the sheets on a disk the server can read and create a
job with `folder=` - nothing is uploaded and the server reads with all cores.
Use uploads (`files=` / `upload_job_files` in chunks of ~50, then `start_job`)
when the client and server are on different machines.

```bash
# Python 3.8+ (copy clients/python/omr_client.py into your project)
python clients/python/bulk_folder.py --url http://127.0.0.1:8000 --template exam --folder /data/day1 --out day1.csv

# Java 11+
cd clients/java && mvn -q package && java -jar target/omr-client-1.0.0.jar --template exam --folder /data/day1 --out day1.csv

# Go 1.18+
cd clients/go/omrclient && go run ./examples/bulk -template exam -folder /data/day1 -out day1.csv

# Throughput through the API (starts its own server on synthetic sheets)
python clients/python/api_benchmark.py --n 300 --preset phone --workers 8
```

`openapi.json` is the full API description (regenerate with
`python clients/export_openapi.py`); feed it to openapi-generator / oapi-codegen
for other languages. Interactive docs: `http://<server>/docs`.

Tests: `pytest src/tests/test_clients.py` starts a server and runs all three
clients against it (Go/Java parts are skipped if the toolchain is missing).
