"""
Barcode and 2D code reading: a chain of decoders tried in order until one reads.

1. ``zxing``: ZXing-C++, the reference open-source decoder, with progressively
   heavier preprocessing. It reads every symbology it supports, including
   Code 128, Code 39/93, Codabar, EAN-8/13, UPC-A/E, ITF, GS1 DataBar, PDF417,
   MicroPDF417, QR Code (all versions, Model 1/2), Micro QR, rMQR, Data Matrix,
   Aztec and MaxiCode.
2. ``builtin``: the pure-numpy scanline decoder in src/readers/linear.py
   (Code 128, Code 39, ITF, EAN-13/8, UPC-A). No native dependency.
3. ``opencv``: OpenCV's QR detector (QR zones, or barcode zones that allow QR).
4. ``pyzbar``: ZBar through pyzbar, only when installed *and* switched on
   (``"pyzbar": true`` in config barcode_params or the zone options).

Later engines run only when every earlier one read nothing, so a sheet whose
code ZXing reads costs nothing extra. The zone result records the engine in
``engine``; a read by anything other than ZXing adds the flag
``decoded_by_fallback`` (sent to review only if ``review_fallback_decodes``).

A zone can restrict symbologies with options.formats, e.g. ["Code128"], and
the engine order with options.engines.
"""

import cv2
import numpy as np

from src.readers import linear
from src.readers.base import ZoneReadResult

try:
    import zxingcpp
except ImportError:  # pragma: no cover - exercised only without the dependency
    zxingcpp = None

try:  # optional: ZBar (needs the zbar shared library; bundled in Windows wheels)
    from pyzbar import pyzbar
except Exception:  # pragma: no cover - ImportError, or OSError without libzbar
    pyzbar = None

from src.readers.zxing_compat import apply as _apply_zxing_compat
from src.readers.zxing_compat import format_label

if _apply_zxing_compat() or _apply_zxing_compat.patched:
    # zxing-cpp 2.2 reports 0-prefixed EAN-13 codes as 12-digit UPC-A
    linear.UPCA_AS_EAN13 = False

DEFAULT_ENGINES = ["zxing", "builtin", "opencv", "pyzbar"]
PRIMARY_ENGINE = "zxing"
QR_FORMATS = {"qrcode", "microqrcode", "rmqrcode", "matrixcodes", "all", "any"}
# pyzbar type names -> zxing-cpp >= 2.3 labels
_PYZBAR_LABELS = {
    "CODE128": "Code 128",
    "CODE39": "Code 39",
    "CODE93": "Code 93",
    "CODABAR": "Codabar",
    "EAN13": "EAN-13",
    "EAN8": "EAN-8",
    "UPCA": "UPC-A",
    "UPCE": "UPC-E",
    "I25": "ITF",
    "DATABAR": "DataBar",
    "DATABAR_EXP": "DataBar Expanded",
    "PDF417": "PDF417",
    "QRCODE": "QR Code",
}


def available_engines():
    """Which barcode engines can run in this install."""
    return {
        "zxing": zxingcpp is not None,
        "builtin": True,
        "opencv": True,
        "pyzbar": pyzbar is not None,
    }


def supported_formats():
    if zxingcpp is None:
        return []
    return [str(f) for f in zxingcpp.barcode_formats_list(zxingcpp.BarcodeFormat.All)]


def _formats_option(zone):
    names = zone.options.get("formats")
    if not names:
        return None
    return zxingcpp.barcode_formats_from_str(",".join(names))


def _attempts(crop):
    """Progressively heavier preprocessing, tried until something decodes."""
    yield crop
    h, w = crop.shape[:2]
    if max(h, w) < 1200:
        yield cv2.resize(crop, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
    _, otsu = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield otsu
    yield cv2.GaussianBlur(crop, (3, 3), 0)
    # Blurred/noisy 1-D codes: average along the bars, re-sharpen across them.
    # Bars may run either way (codes printed sideways), so try both.
    for turned in (crop, cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)):
        averaged = cv2.blur(turned, (1, 15))
        sharpened = cv2.addWeighted(
            averaged, 2.0, cv2.GaussianBlur(averaged, (0, 0), 2), -1.0, 0
        )
        yield cv2.resize(sharpened, None, fx=3, fy=1, interpolation=cv2.INTER_CUBIC)
        yield cv2.createCLAHE(2.0, (8, 2)).apply(averaged)


def decode_symbols(crop, formats=None):
    if zxingcpp is None:
        return None
    kwargs = {"formats": formats} if formats is not None else {}
    for attempt in _attempts(crop):
        symbols = zxingcpp.read_barcodes(attempt, **kwargs)
        if symbols:
            return symbols
    return []


def _norm(name):
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def engine_order(zone, params=None):
    """Engines to try for this zone, in order (pyzbar only when switched on)."""
    params = params or {}
    options = zone.options
    order = options.get("engines") or params.get("engines") or DEFAULT_ENGINES
    pyzbar_on = options.get("pyzbar", params.get("pyzbar", False))
    return [name for name in order if name != "pyzbar" or pyzbar_on]


def _wants_qr(zone):
    if zone.type == "qrcode":
        return True
    names = zone.options.get("formats")
    return not names or any(_norm(n) in QR_FORMATS for n in names)


def _symbol(text, fmt, valid=True):
    return {"text": text, "format": fmt, "valid": bool(valid)}


def _read_zxing(zone, crop):
    if zxingcpp is None:
        return None
    formats = _formats_option(zone)
    if formats is None and zone.type == "qrcode":
        formats = zxingcpp.barcode_formats_from_str("QRCode,MicroQRCode,RMQRCode")
    symbols = decode_symbols(crop, formats) or []
    details = {}
    if symbols:
        details["orientation"] = int(symbols[0].orientation)
    return [_symbol(s.text, format_label(s.format), s.valid) for s in symbols], details


def _read_builtin(zone, crop):
    if zone.type == "qrcode":
        return None
    formats = zone.options.get("formats")
    if not linear.builtin_formats(formats):
        return None
    found = linear.decode(crop, formats, zone.options)
    if found is None:
        return [], {}
    return [_symbol(found["text"], found["format"])], {
        "scanline_votes": found["votes"],
        "rotated": found["rotated"],
    }


def _opencv_qr_detectors():
    detectors = []
    if hasattr(cv2, "QRCodeDetectorAruco"):
        detectors.append(cv2.QRCodeDetectorAruco())
    detectors.append(cv2.QRCodeDetector())
    return detectors


def _read_opencv(zone, crop):
    if not _wants_qr(zone):
        return None
    padded = cv2.copyMakeBorder(crop, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
    h, w = padded.shape[:2]
    attempts = [padded]
    if max(h, w) < 600:
        attempts.append(
            cv2.resize(padded, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        )
    for detector in _opencv_qr_detectors():
        for attempt in attempts:
            try:
                text, points, _ = detector.detectAndDecode(attempt)
            except cv2.error:
                continue
            if text:
                return [_symbol(text, "QR Code")], {}
    return [], {}


def _read_pyzbar(zone, crop):
    if pyzbar is None:
        return None
    wanted = {_norm(n) for n in zone.options.get("formats") or []}
    if zone.type == "qrcode":
        wanted = wanted or {"qrcode"}
    symbols = []
    for decoded in pyzbar.decode(crop):
        label = _PYZBAR_LABELS.get(decoded.type, decoded.type)
        if wanted and _norm(label) not in wanted and _norm(decoded.type) not in wanted:
            continue
        symbols.append(_symbol(decoded.data.decode("utf-8", "replace"), label))
    return symbols, {}


ENGINE_READERS = {
    "zxing": _read_zxing,
    "builtin": _read_builtin,
    "opencv": _read_opencv,
    "pyzbar": _read_pyzbar,
}


def read_barcode_zone(zone, image, params=None):
    params = params or {}
    crop = zone.crop(image, padding=10)
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    tried = []
    symbols, details, engine = [], {}, None
    for name in engine_order(zone, params):
        reader = ENGINE_READERS.get(name)
        outcome = reader(zone, crop) if reader else None
        if outcome is None:
            continue  # not installed, or not applicable to this zone
        tried.append(name)
        symbols, details = outcome
        if symbols:
            engine = name
            break
    if not tried:
        return ZoneReadResult(zone.name, zone.type, "", 0.0, ["engine_unavailable"])
    if not symbols:
        return ZoneReadResult(
            zone.name, zone.type, "", 0.0, ["not_found"], details={"engines": tried}
        )

    flags = []
    if len(symbols) > 1:
        flags.append("multiple_symbols")
    if engine != PRIMARY_ENGINE:
        flags.append("decoded_by_fallback")
    symbol = symbols[0]
    # Every engine only returns symbols whose checksum/error correction passed
    confidence = 1.0 if symbol["valid"] else 0.0
    return ZoneReadResult(
        zone.name,
        zone.type,
        symbol["text"],
        confidence,
        flags,
        format=symbol["format"],
        details={"symbols": symbols, "engines": tried, **details},
        engine=engine,
    )


def read_all_symbols(image, formats=None):
    """Find every barcode/QR on a full page (used by auto template generation)."""
    if zxingcpp is None:
        return []
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    kwargs = {"formats": formats} if formats is not None else {}
    results = []
    for symbol in zxingcpp.read_barcodes(gray, **kwargs):
        pos = symbol.position
        pts = np.array(
            [
                [pos.top_left.x, pos.top_left.y],
                [pos.top_right.x, pos.top_right.y],
                [pos.bottom_right.x, pos.bottom_right.y],
                [pos.bottom_left.x, pos.bottom_left.y],
            ]
        )
        x, y = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        results.append(
            {
                "text": symbol.text,
                "format": format_label(symbol.format),
                "box": [int(x), int(y), int(x1 - x), int(y1 - y)],
            }
        )
    return results
