"""
Barcode and 2D code reading using ZXing-C++, the reference open-source decoder.

Every symbology ZXing-C++ supports is accepted by default, including Code 128,
Code 39/93, Codabar, EAN-8/13, UPC-A/E, ITF, GS1 DataBar, PDF417, MicroPDF417,
QR Code (all versions, Model 1/2), Micro QR, rMQR, Data Matrix, Aztec and
MaxiCode. A zone can restrict this with options.formats, e.g. ["Code128"].
"""

import cv2
import numpy as np

from src.readers.base import ZoneReadResult

try:
    import zxingcpp
except ImportError:  # pragma: no cover - exercised only without the dependency
    zxingcpp = None

from src.readers.zxing_compat import apply as _apply_zxing_compat
from src.readers.zxing_compat import format_label

_apply_zxing_compat()


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
    # Blurred/noisy 1-D codes: average along the bars, re-sharpen across them
    averaged = cv2.blur(crop, (1, 15))
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


def read_barcode_zone(zone, image):
    crop = zone.crop(image, padding=10)
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if zxingcpp is None:
        return ZoneReadResult(zone.name, zone.type, "", 0.0, ["engine_unavailable"])

    formats = _formats_option(zone)
    if formats is None and zone.type == "qrcode":
        formats = zxingcpp.barcode_formats_from_str("QRCode,MicroQRCode,RMQRCode")
    symbols = decode_symbols(crop, formats)
    if not symbols:
        return ZoneReadResult(zone.name, zone.type, "", 0.0, ["not_found"])

    flags = []
    if len(symbols) > 1:
        flags.append("multiple_symbols")
    symbol = symbols[0]
    # ZXing only returns symbols whose checksum/error correction succeeded
    confidence = 1.0 if symbol.valid else 0.0
    return ZoneReadResult(
        zone.name,
        zone.type,
        symbol.text,
        confidence,
        flags,
        format=format_label(symbol.format),
        details={
            "symbols": [
                {
                    "text": s.text,
                    "format": format_label(s.format),
                    "valid": bool(s.valid),
                }
                for s in symbols
            ],
            "orientation": int(symbol.orientation),
        },
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
