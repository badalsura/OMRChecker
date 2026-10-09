# Portable Windows build (Windows 7 SP1 and later)

One folder (or one exe) that runs the OMR web GUI, the REST API and the headless
bulk reader, with nothing to install on the target PC.

| Output | What it is |
| --- | --- |
| `dist\OMRChecker\OMRChecker.exe` (+ files) | **Recommended.** Portable folder; zip it and copy anywhere. Starts fast, and bulk worker processes start fast. |
| `dist\OMRChecker-portable-win64.zip` | The folder above, zipped. |
| `dist\OMRChecker-onefile.exe` | A single exe. Every start (and every worker process) unpacks itself to `%TEMP%`, so it is slower for bulk jobs. |

## Using it

```
OMRChecker.exe                                 # API + GUI on http://127.0.0.1:8765/, opens the browser
OMRChecker.exe --host 0.0.0.0 --port 8000      # serve the LAN (other PCs, phones, the Python/Java/Go clients)
OMRChecker.exe --api-key SECRET                # require X-API-Key on every API call
OMRChecker.exe --bulk D:\scans --template D:\exam\template.json --out D:\results --workers 8
OMRChecker.exe --selftest                      # headless check, exit code 0 = OK
OMRChecker.exe --version
```

* Data (templates, scan results, review queue, training labels) is kept in
  `omr_data\` next to the exe. If that folder is read-only (e.g. under
  `C:\Program Files`) it falls back to `%LOCALAPPDATA%\OMRChecker\omr_data`.
  `--data-dir` overrides both.
* A small window shows the URL with **Open GUI**, **Copy URL** and **Quit**.
  Closing it stops the server. `--no-window` runs as a plain console server.
* `--bulk` writes `results.csv` and `results.jsonl`; rows with
  `status = needs_review` / `error` or a non-empty `needs_review` column are the
  ones to check by hand (or load the folder as a job in the GUI to use the
  review screen).
* The in-browser reader (`web/omr-browser`) is bundled and served at `/browser/`.

### The launcher window

| Control | What it does |
| --- | --- |
| Port, "Other devices on this network can connect" | Where the server listens (unticked: this PC only). Applied by Start / Restart server. |
| Start (Restart) server, Stop server, Open GUI | The local server; the address has a Copy button. Stopping it also stops remote access. |
| Start remote, Stop remote | Remote access through a Cloudflare Tunnel with the bundled `cloudflared`; the address has a Copy button. |
| Cloudflare settings… | Empty: a **quick tunnel** (a new random `https://….trycloudflare.com` address each start, no account). For a fixed address on your domain enter either a **tunnel token** (Zero Trust › Networks › Tunnels; point its public hostname at `http://localhost:<port>`) or a **Cloudflare API token + hostname** (the station creates the tunnel `omrchecker-<hostname>`, routes it to the current port and adds the DNS record through the Cloudflare API; token permissions: Account › Cloudflare Tunnel: Edit, Zone › DNS: Edit). Saved in `omr_data\remote_access.json`. |
| Quit | Stops remote access and the server, and closes the app. |

`OMRChecker.exe --remote` starts remote access at launch as well (handy with
`--no-window`; the address is printed). Turn on sign-in (create the admin
account) before sharing a remote address.

Current `cloudflared` builds need **Windows 10 or newer** (Go dropped Windows
7). On Windows 7, Start remote reports that cloudflared failed; run the tunnel
from a newer PC on the same network, or replace `cloudflared\cloudflared.exe`
with an older release (Cloudflare may refuse releases older than a year).

### Browser requirements

The GUI uses ES modules plus optional chaining (`?.`), nullish coalescing (`??`)
and `Object.fromEntries`, so it needs **Chrome 80+, Edge 80+ (Chromium),
Firefox 74+ or Opera 67+**. Internet Explorer and old (EdgeHTML) Edge are not
supported. On Windows 7 use **Chrome 109** (the last Chrome for Win7) or
**Firefox ESR 115** (the last Firefox for Win7); both work.

### What every build bundles

`packaging\prepare_bundle.py` (run by `build_windows.bat`) and the GitHub build
always include, or stop the build:

* Tesseract (`tesseract.exe` + DLLs) with the "best" English, Hindi and Punjabi models
* PaddleOCR PP-OCRv5 mobile (English + Devanagari), used for OCR fallback and
  to read handwriting (ICR zones) when no ICR model is loaded
* `cloudflared.exe` for remote access

A local build copies Tesseract from an installed UB Mannheim build (installing
it with winget if missing) and converts the PaddleOCR models with a Python 3.9+
next to 3.8 (`py -3.11`; paddle2onnx has no 3.8 wheels).

### OCR (Tesseract)

`tesserocr` has no Windows wheels, so the Windows build uses `pytesseract`,
which runs `tesseract.exe`. The GitHub Actions build (`build-windows.yml`)
bundles it automatically: it installs the UB Mannheim build with Chocolatey and
copies `tesseract.exe`, its DLLs and the English/OSD data into
`packaging\tesseract\`. For a local build, bundle it by hand:

1. Install the UB Mannheim build (https://github.com/UB-Mannheim/tesseract/wiki;
   the 5.x installers run on Windows 7 x64) and copy its install folder
   (`tesseract.exe`, the DLLs and `tessdata\`) to `packaging\tesseract\` before
   building, **or** copy it to a `tesseract\` folder next to `OMRChecker.exe`
   after building.
2. The launcher looks in `<exe dir>\tesseract`, then `C:\Program Files\Tesseract-OCR`,
   puts it on `PATH` and sets `TESSDATA_PREFIX`.

Without Tesseract, OCR zones go to manual review; bubbles, barcodes and QR codes
are unaffected. `--selftest` prints `"tesseract": true/false`.

Model choice (Tesseract "best" or "fast", extra languages such as Hindi,
PaddleOCR mobile/server as default or fallback engine): run
`python packaging/fetch_ocr_models.py` before building; see
[ocr_models.md](ocr_models.md).

### Learned models (ONNX Runtime) on Windows 7

ONNX Runtime officially supports Windows 10+ only. Its last Python 3.8 build
(1.19.2) is bundled; on Windows 7 it may need updates KB2999226 (Universal C
Runtime), KB3068708 and KB3080149 before it imports. If it cannot load, the
engine keeps working: bubbles use classical adaptive thresholding and ICR zones
are flagged for review. `--selftest` reports `"onnxruntime": "unavailable (...)"`
in that case. On Windows 10/11 it works as is.

### Barcodes without native decoders: built-in and optional ZBar

Barcode zones try ZXing-C++ first, then a built-in pure-NumPy decoder (Code 128,
Code 39, ITF, EAN/UPC) and OpenCV's QR detector, so a barcode is still read if
the zxing-cpp DLL cannot load. `--selftest` reports `"barcode_engines"`.

pyzbar (ZBar) is bundled as an optional fourth engine and is **off** unless
`barcode_params.pyzbar` is `true` in `config.json`. Its `libzbar-64.dll` needs
the **Visual C++ 2013 x64 redistributable** (`vcredist_x64.exe`, msvcr120.dll),
which PyInstaller does not bundle. Without it pyzbar does not load and
`barcode_engines.pyzbar` is `false`; the other engines are unaffected.

### Windows 7 prerequisites

* Windows 7 **SP1 x64** with **KB2533623** (needed by Python 3.8 itself) and
  **KB2999226** (Universal C Runtime, used by OpenCV/NumPy wheels). Both are in
  any fully updated Win7 install.
* Visual C++ 2015-2022 x64 redistributable is bundled by PyInstaller
  (`vcruntime140.dll`, `msvcp140.dll`); if a DLL error mentions `VCRUNTIME140_1`
  install `vc_redist.x64.exe` from Microsoft.
* Windows 7 "N"/"KN" editions additionally need the Media Feature Pack for OpenCV.

### Optional features on the Windows 7 build

The portable exe stays pinned to Python 3.8 and the libraries in
`requirements-win7.txt`. Every optional engine switches itself off cleanly when
it cannot load there, and the feature that needs it falls back (OCR zones and
ICR go to review, bubbles use thresholding, barcodes use the built-in decoder).
`--selftest` prints an `"engines"` block, and `GET /health` returns `"engines"`,
saying which ones loaded:

```
"engines": {
  "onnxruntime": {"available": true,  "detail": "1.19.2"},
  "tesseract":   {"available": false, "detail": "not found; OCR zones go to review"},
  "paddleocr":   {"available": false, "detail": "not included in this build"},
  "zxing": {...}, "pyzbar": {...}, "xlsx": {...}, "pdf": {...}, "sql": {...}
}
```

## Docker server (optional, not for Windows 7)

**Docker Desktop does not run on Windows 7** (it needs Windows 10 or 11, and
Docker Toolbox for Windows 7 was discontinued years ago). The Docker image is for
sites with **one newer machine** (Windows 10/11 with Docker Desktop, or a Linux
server): it runs the full server with current Python, Tesseract (English and
Hindi/Devanagari), ONNX Runtime and every export. The Windows 7 PCs then use it
through the browser, with **Chrome 109** or **Firefox ESR 115** (the last
versions for Windows 7), at `http://<server>:8000/`. No install on the Win7 PCs.

```
docker compose up -d --build          # from the repository root
docker compose logs -f omr
curl http://localhost:8000/health     # "engines" lists what loaded
```

* Data lives in the `omr_data` volume (`/data` in the container).
* Put scan folders in `./scans` (mounted read-only at `/scans`, the only folder
  server-side jobs may read; `OMR_ALLOWED_DIRS`).
* Optional ONNX models (bubble, ICR, PaddleOCR) go in `./models`, mounted at
  `/app/models`; point `config.json` at them.
* Set `OMR_API_KEY` (e.g. in a `.env` file next to `docker-compose.yml`) on a
  shared network. Open port 8000 in the host firewall for the Win7 PCs.

## Release checklist: test on a real Windows 7 machine

CI runs on Windows Server 2022, and the build has never been run on Windows 7.
Before each release, on a real **Windows 7 SP1 x64** PC (not a VM snapshot of a
newer Windows; a Win7 VM is acceptable if no PC is available):

- [ ] KB2533623 and KB2999226 installed; note whether KB3068708/KB3080149 are.
- [ ] Unzip `OMRChecker-portable-win64.zip` to a folder with no admin rights
      (e.g. the Desktop) and run `OMRChecker.exe --version`.
- [ ] `OMRChecker.exe --selftest` exits 0. Record its `"engines"` block in the
      release notes (onnxruntime, tesseract, paddleocr, zxing, pyzbar).
- [ ] For each engine that is `false`, confirm the feature falls back as
      described above and the GUI shows a clear message instead of an error.
- [ ] Start `OMRChecker.exe`; the control window opens; the GUI loads in
      **Chrome 109** and in **Firefox ESR 115**. Scan one sample sheet, open the
      Results tab overlay and the review screen (colour toggle, full colour view).
- [ ] `OMRChecker.exe --bulk samples\... --template ... --workers 2` writes
      `results.csv`; the onefile exe does the same.
- [ ] With Tesseract copied next to the exe, an OCR zone reads; without it, the
      zone goes to review.
- [ ] From the Win7 browser, open a Docker server (if the site uses one) and run
      one job through it.
- [ ] Note the Windows build, browser versions and any missing updates in the
      release notes.

## Building

Build on Windows (7 SP1, 10 or 11, x64) with **Python 3.8.10 x64**
(https://www.python.org/downloads/release/python-3810/ - the last 3.8 installer,
and the newest Python that runs on Windows 7). An exe built with Python 3.9+
will not start on Windows 7.

```
packaging\build_windows.bat            :: onedir + onefile, then --version / --selftest
packaging\build_windows.bat onedir     :: only the portable folder
```

The script creates `build\venv38`, installs `packaging\requirements-win7.txt`
(every package pinned to its newest release with a CPython 3.8 Windows wheel;
check with `python packaging\verify_wheels.py`), runs PyInstaller 5.13.2 on
`packaging\omr.spec`, smoke-tests the result and zips the folder.

Manual equivalent:

```
py -3.8 -m venv build\venv38 && build\venv38\Scripts\activate
pip install -r packaging\requirements-win7.txt
set OMR_BUILD_MODE=onedir        & rem onedir | onefile | both
set OMR_CONSOLE=1                & rem 0 = no console window
pyinstaller --noconfirm --clean packaging\omr.spec
dist\OMRChecker\OMRChecker.exe --selftest
```

Optional: put an icon at `packaging\omr.ico`, and Tesseract at `packaging\tesseract\`.

GitHub Actions (`.github/workflows/build-windows.yml`) does the same on every
`v*` tag or manual run and uploads the zip and the onefile exe as artifacts.
Note: GitHub's runners are Windows Server 2022, so CI proves the build and the
self test, not Windows 7 itself; run `OMRChecker.exe --selftest` once on a real
Windows 7 machine before rolling out.

### Linux / source

The same spec builds a Linux bundle (`pyinstaller packaging/omr.spec`; add
`OMR_NO_TK=1` when the Python has a statically linked Tcl/Tk, e.g. uv's
standalone builds), and the launcher runs from a checkout:
`python packaging/omr_desktop.py --selftest`. The spec was validated this way
with CPython 3.8 and the exact pins above (onedir and onefile: `--selftest`,
`--bulk`, and server mode driven by the Python client).

## Barcodes on the Windows 7 build

zxing-cpp 2.2.0 is the last release with Python 3.8 wheels. It reads Code 128,
Code 39/93, Codabar, EAN-8/13, UPC-A/E, ITF, DataBar (+Expanded), PDF417,
QR Code, Micro QR, rMQR, Data Matrix, Aztec and MaxiCode. `src/readers/zxing_compat.py`
back-fills the newer zxing-cpp API the engine uses.
Differences from the Python 3.10+ build (zxing-cpp 3.x):

* Codabar text comes back without its start/stop characters (`123456`, not `A123456A`).
* Newer-only symbologies (DX Film Edge, Telepen, Code 32, ...) are not available.
* Micro QR cannot be *generated* for synthetic test sheets (reading works).

## Exports and the Results screen

The build bundles everything that exports need:
- openpyxl for XLSX;
- reportlab 4.4.2 for PDF (4.4.3 and later need Python 3.9);
- SQLAlchemy with its SQLite dialect for SQL.

PostgreSQL exports need `psycopg[binary]`, which is not bundled. Use a source
install for that, or export to `.sqlite` or CSV and load the file.

```
OMRChecker.exe --host 0.0.0.0                 # Results tab: http://<pc>:8765/#results
set OMR_PATH_REMAP=D:\scans=E:\archive\scans   # sheets whose input folder moved
set OMR_PDF_SHEET_LIMIT=2000                  # cap on per-sheet PDF pages
```

The Results tab re-reads each sheet from its original file to draw the overlay,
so no image is stored per sheet. If you move or archive the input folders, add a
path remap: **Results > Path remap**, per job, or with `OMR_PATH_REMAP`. Only
sheets whose file is gone and that have no stored image (`save_images=all`)
cannot be shown.

## Notes for high-volume use

* Use the onedir build for bulk work: worker processes of a onefile exe each
  unpack the whole bundle first.
* `--workers` defaults to all cores. Measured end to end through the API
  (server-side folder job, 300 synthetic phone photos with timing-mark
  registration, 4-core Linux box): ~2,960 sheets/min with `save_images=none`,
  ~1,310 sheets/min with the default `save_images=review` (61% of these noisy
  phone photos were flagged, so their images were written). Expect roughly 2x
  on an 8-core PC. Measure yours with `clients/python/api_benchmark.py`.
* Exclude `omr_data\` and the exe folder from real-time antivirus scanning;
  scanning every written result file is the usual bottleneck on Windows.
* Exports stream from the index, so even millions of rows use little memory.
  XLSX starts a new sheet every 1,048,575 rows, and SQL exports upsert in
  batches of 1,000 rows.
