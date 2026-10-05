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

### Browser requirements

The GUI uses ES modules plus optional chaining (`?.`), nullish coalescing (`??`)
and `Object.fromEntries`, so it needs **Chrome 80+, Edge 80+ (Chromium),
Firefox 74+ or Opera 67+**. Internet Explorer and old (EdgeHTML) Edge are not
supported. On Windows 7 use **Chrome 109** (the last Chrome for Win7) or
**Firefox ESR 115** (the last Firefox for Win7); both work.

### OCR (Tesseract)

`tesserocr` has no Windows wheels, so the Windows build uses `pytesseract`,
which runs `tesseract.exe`. To bundle it:

1. Install the UB Mannheim build (https://github.com/UB-Mannheim/tesseract/wiki;
   the 5.x installers run on Windows 7 x64) and copy its install folder
   (`tesseract.exe`, the DLLs and `tessdata\`) to `packaging\tesseract\` before
   building, **or** copy it to a `tesseract\` folder next to `OMRChecker.exe`
   after building.
2. The launcher looks in `<exe dir>\tesseract`, then `C:\Program Files\Tesseract-OCR`,
   puts it on `PATH` and sets `TESSDATA_PREFIX`.

Without Tesseract, OCR zones go to manual review; bubbles, barcodes and QR codes
are unaffected. `--selftest` prints `"tesseract": true/false`.

### Learned models (ONNX Runtime) on Windows 7

ONNX Runtime officially supports Windows 10+ only. Its last Python 3.8 build
(1.19.2) is bundled; on Windows 7 it may need updates KB2999226 (Universal C
Runtime), KB3068708 and KB3080149 before it imports. If it cannot load, the
engine keeps working: bubbles use classical adaptive thresholding and ICR zones
are flagged for review. `--selftest` reports `"onnxruntime": "unavailable (...)"`
in that case. On Windows 10/11 it works as is.

### Windows 7 prerequisites

* Windows 7 **SP1 x64** with **KB2533623** (needed by Python 3.8 itself) and
  **KB2999226** (Universal C Runtime, used by OpenCV/NumPy wheels). Both are in
  any fully updated Win7 install.
* Visual C++ 2015-2022 x64 redistributable is bundled by PyInstaller
  (`vcruntime140.dll`, `msvcp140.dll`); if a DLL error mentions `VCRUNTIME140_1`
  install `vc_redist.x64.exe` from Microsoft.
* Windows 7 "N"/"KN" editions additionally need the Media Feature Pack for OpenCV.

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
