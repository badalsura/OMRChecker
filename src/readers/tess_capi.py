"""
In-process Tesseract through its C API (ctypes), for installs without the
tesserocr binding (the Windows builds). Same engine and models as the
tesseract executable that pytesseract runs, without starting a process and
writing a temporary image per zone (~3x faster on OCR-heavy sheets).

Used only when the library loads; every failure falls back to pytesseract.
"""

import ctypes
import ctypes.util
import glob
import os
import shutil
import sys
import threading
from pathlib import Path

import numpy as np

RIL_WORD = 3
RIL_SYMBOL = 4
OEM_LSTM_ONLY = 1

_LIB = None
_LIB_TRIED = False
_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()


def _candidates():
    env = os.environ.get("OMR_LIBTESSERACT")
    if env:
        yield env
    dirs = []
    exe = shutil.which("tesseract")
    try:
        import pytesseract

        cmd = pytesseract.pytesseract.tesseract_cmd
        if cmd and os.path.isabs(cmd):
            exe = cmd
    except Exception:  # pragma: no cover
        pass
    if exe:
        dirs.append(Path(exe).resolve().parent)
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent / "tesseract")
    dirs.append(Path(__file__).resolve().parents[2] / "packaging" / "tesseract")
    for directory in dirs:
        for pattern in ("libtesseract*.dll", "tesseract*.dll", "libtesseract*.so*", "libtesseract*.dylib"):
            yield from sorted(glob.glob(str(directory / pattern)), reverse=True)
    found = ctypes.util.find_library("tesseract")
    if found:
        yield found
    for name in ("libtesseract.so.5", "libtesseract.so.4"):
        yield name


def library():
    """The loaded libtesseract with argument types set, or None."""
    global _LIB, _LIB_TRIED
    with _LOCK:
        if _LIB_TRIED:
            return _LIB
        _LIB_TRIED = True
        if os.environ.get("OMR_TESSERACT_CAPI", "1") == "0":
            return None
        for path in _candidates():
            try:
                if sys.platform == "win32" and os.path.isabs(path):
                    # Its sibling DLLs (leptonica, ...) live in the same folder
                    os.environ["PATH"] = str(Path(path).parent) + os.pathsep + os.environ.get("PATH", "")
                    if hasattr(os, "add_dll_directory"):
                        os.add_dll_directory(str(Path(path).parent))
                lib = ctypes.CDLL(path)
                _declare(lib)
                _LIB = lib
                break
            except (OSError, AttributeError):
                continue
        return _LIB


def _declare(lib):
    vp, cp, ci = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
    lib.TessBaseAPICreate.restype = vp
    lib.TessBaseAPIInit4.argtypes = [
        vp, cp, cp, ci, ctypes.POINTER(cp), ci,
        ctypes.POINTER(cp), ctypes.POINTER(cp), ctypes.c_size_t, ci,
    ]
    lib.TessBaseAPIInit4.restype = ci
    lib.TessBaseAPISetPageSegMode.argtypes = [vp, ci]
    lib.TessBaseAPISetVariable.argtypes = [vp, cp, cp]
    lib.TessBaseAPISetVariable.restype = ci
    lib.TessBaseAPISetImage.argtypes = [vp, ctypes.c_void_p, ci, ci, ci, ci]
    lib.TessBaseAPIRecognize.argtypes = [vp, vp]
    lib.TessBaseAPIRecognize.restype = ci
    lib.TessBaseAPIGetIterator.argtypes = [vp]
    lib.TessBaseAPIGetIterator.restype = vp
    lib.TessResultIteratorGetUTF8Text.argtypes = [vp, ci]
    lib.TessResultIteratorGetUTF8Text.restype = ctypes.POINTER(ctypes.c_char)
    lib.TessResultIteratorConfidence.argtypes = [vp, ci]
    lib.TessResultIteratorConfidence.restype = ctypes.c_float
    lib.TessResultIteratorNext.argtypes = [vp, ci]
    lib.TessResultIteratorNext.restype = ci
    lib.TessResultIteratorDelete.argtypes = [vp]
    lib.TessDeleteText.argtypes = [ctypes.POINTER(ctypes.c_char)]
    lib.TessBaseAPIClear.argtypes = [vp]
    lib.TessBaseAPIDelete.argtypes = [vp]


def available(tessdata):
    return tessdata is not None and library() is not None


def _handle(tessdata, lang, psm, patterns_file):
    """One initialised engine per thread and setting (Init is the slow part)."""
    handles = getattr(_THREAD_LOCAL, "handles", None)
    if handles is None:
        handles = _THREAD_LOCAL.handles = {}
    key = (tessdata, lang, psm, patterns_file)
    if key in handles:
        return handles[key]
    lib = library()
    handle = lib.TessBaseAPICreate()
    names, values = [], []
    if patterns_file:
        names.append(b"user_patterns_file")
        values.append(patterns_file.encode())
    n = len(names)
    names_arr = (ctypes.c_char_p * max(n, 1))(*names)
    values_arr = (ctypes.c_char_p * max(n, 1))(*values)
    datapath = tessdata.rstrip("/\\") + os.sep
    status = lib.TessBaseAPIInit4(
        handle, datapath.encode(), (lang or "eng").encode(), OEM_LSTM_ONLY,
        None, 0, names_arr, values_arr, n, 0,
    )
    if status != 0:
        lib.TessBaseAPIDelete(handle)
        raise RuntimeError(f"Tesseract could not load '{lang}' from {tessdata}")
    lib.TessBaseAPISetPageSegMode(handle, int(psm))
    handles[key] = handle
    return handle


def _iterate(lib, handle, level):
    out = []
    iterator = lib.TessBaseAPIGetIterator(handle)
    if not iterator:
        return out
    try:
        while True:
            pointer = lib.TessResultIteratorGetUTF8Text(iterator, level)
            if pointer:
                text = ctypes.string_at(pointer).decode("utf-8", "replace")
                lib.TessDeleteText(pointer)
                if text.strip():
                    out.append((text.strip(), lib.TessResultIteratorConfidence(iterator, level) / 100.0))
            if not lib.TessResultIteratorNext(iterator, level):
                break
    finally:
        lib.TessResultIteratorDelete(iterator)
    return out


def recognize(image, psm, whitelist, lang, tessdata, patterns_file=None):
    """(text, mean word confidence, per-character confidences) like tesserocr."""
    lib = library()
    handle = _handle(tessdata, lang, psm, patterns_file)
    image = np.ascontiguousarray(image, dtype=np.uint8)
    if image.ndim == 3:
        image = np.ascontiguousarray(image[:, :, ::-1])  # BGR -> RGB
        bpp = 3
    else:
        bpp = 1
    h, w = image.shape[:2]
    lib.TessBaseAPISetVariable(handle, b"tessedit_char_whitelist", (whitelist or "").encode())
    lib.TessBaseAPISetImage(handle, image.ctypes.data, w, h, bpp, int(image.strides[0]))
    if lib.TessBaseAPIRecognize(handle, None) != 0:
        lib.TessBaseAPIClear(handle)
        return "", 0.0, []
    words = _iterate(lib, handle, RIL_WORD)
    chars = [c for _, c in _iterate(lib, handle, RIL_SYMBOL)] if words else []
    lib.TessBaseAPIClear(handle)
    if not words:
        return "", 0.0, []
    return " ".join(t for t, _ in words), float(np.mean([c for _, c in words])), chars
