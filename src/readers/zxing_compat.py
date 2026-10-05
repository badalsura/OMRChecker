"""
Back-fill the zxing-cpp >= 2.3 Python API on zxing-cpp 2.2.0.

zxing-cpp 2.2.0 is the last release with CPython 3.8 wheels, and Python 3.8 is
the newest Python that runs on Windows 7. The engine (src/readers/barcode.py,
src/synth/render.py) is written against the newer API and calls apply() on
import. On newer zxing-cpp it does nothing.

Decoding is identical: 2.2.0 reads every symbology the engine relies on
(Code 128, Code 39/93, Codabar, EAN/UPC, ITF, DataBar, PDF417, QR Code incl.
Micro QR and rMQR, Data Matrix, Aztec, MaxiCode).
"""

import numpy as np

_FORMAT_LABELS = {
    # zxing-cpp >= 2.3 str() spellings, so outputs match across builds
    "Aztec": "Aztec",
    "Codabar": "Codabar",
    "Code39": "Code 39",
    "Code93": "Code 93",
    "Code128": "Code 128",
    "DataMatrix": "Data Matrix",
    "EAN8": "EAN-8",
    "EAN13": "EAN-13",
    "ITF": "ITF",
    "MaxiCode": "MaxiCode",
    "PDF417": "PDF417",
    "QRCode": "QR Code",
    "MicroQRCode": "Micro QR Code",
    "RMQRCode": "rMQR Code",
    "DataBar": "DataBar",
    "DataBarExpanded": "DataBar Expanded",
    "UPCA": "UPC-A",
    "UPCE": "UPC-E",
}
_GROUPS = {"NONE", "LinearCodes", "MatrixCodes", "All", "Any"}


def format_label(fmt):
    """str(BarcodeFormat) with the zxing-cpp >= 2.3 spelling ("Code 128")."""
    text = str(fmt)
    if text.startswith("BarcodeFormat."):
        name = text[len("BarcodeFormat.") :]
        return _FORMAT_LABELS.get(name, name)
    return text


def apply():
    """Patch the imported zxingcpp module in place. Returns True if it was patched."""
    try:
        import zxingcpp
    except ImportError:
        return False
    if apply.patched or hasattr(zxingcpp, "barcode_formats_list"):
        return False  # already patched, or the new API is present

    members = [
        name for name in zxingcpp.BarcodeFormat.__members__ if name not in _GROUPS
    ]
    all_formats = zxingcpp.barcode_formats_from_str(",".join(members))
    try:
        zxingcpp.BarcodeFormat.All = all_formats
    except (AttributeError, TypeError):
        pass

    def barcode_formats_list(formats=None):
        if formats is None or formats is getattr(zxingcpp.BarcodeFormat, "All", None):
            names = members
        else:
            names = [
                zxingcpp.barcode_format_from_str(part).name
                for part in str(formats).split("|")
                if part
            ]
        # zxing-cpp >= 2.3 spellings ("Code 128", "QR Code") so outputs match
        return [_FORMAT_LABELS.get(name, name) for name in names]

    def create_barcode(text, fmt, **_options):
        return (text, fmt)

    def write_barcode_to_image(symbol, scale=1, **_options):
        text, fmt = symbol
        image = np.array(zxingcpp.write_barcode(fmt, text), dtype=np.uint8)
        if (
            image.ndim == 2 and image.shape[0] == 1
        ):  # linear codes come back one row high
            image = np.repeat(image, max(20, image.shape[1] // 5), axis=0)
        scale = max(int(scale), 1)
        if scale > 1:
            image = np.kron(image, np.ones((scale, scale), dtype=np.uint8))
        return image

    zxingcpp.barcode_formats_list = barcode_formats_list
    zxingcpp.create_barcode = create_barcode
    zxingcpp.write_barcode_to_image = write_barcode_to_image
    apply.patched = True
    return True


apply.patched = False
