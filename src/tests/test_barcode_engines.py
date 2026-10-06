"""Built-in scanline decoder, OpenCV QR fallback, optional pyzbar and the engine chain."""

import random
import string
import time
import types

import cv2
import numpy as np
import pytest

from src.readers import barcode, linear
from src.readers.base import read_zone
from src.template import Zone

zxingcpp = pytest.importorskip("zxingcpp")


def render(text, fmt, scale=2.0, height=60, quiet=12, pad=20):
    """A zxing-generated linear code, resampled to `scale` px per module."""
    symbol = zxingcpp.create_barcode(text, zxingcpp.barcode_format_from_str(fmt))
    image = np.array(zxingcpp.write_barcode_to_image(symbol, scale=1), np.uint8)
    row = image[image.shape[0] // 2]
    row = np.concatenate(
        [np.full(quiet, 255, np.uint8), row, np.full(quiet, 255, np.uint8)]
    )
    image = np.repeat(row[None, :], height, axis=0)
    width = int(round(image.shape[1] * scale))
    image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    return cv2.copyMakeBorder(image, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


def degrade(image, seed, blur=0.0, noise=0.0, rotation=0.0):
    if rotation:
        h, w = image.shape
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), rotation, 1)
        image = cv2.warpAffine(image, matrix, (w, h), borderValue=255)
    if blur:
        image = cv2.GaussianBlur(image, (0, 0), blur)
    if noise:
        rng = np.random.default_rng(seed)
        image = np.clip(image + rng.normal(0, noise, image.shape), 0, 255)
    return image.astype(np.uint8)


def zxing_text(text, fmt):
    """What zxing itself reports for a clean rendering (adds EAN/UPC check digits)."""
    symbols = zxingcpp.read_barcodes(render(text, fmt, 3))
    return symbols[0].text if symbols else None


def decode_text(image, formats=None, **options):
    found = linear.decode(image, formats, options)
    return found and found["text"]


# ---------------------------------------------------------------- tables


def test_code128_table_is_consistent():
    assert len(linear.C128_PATTERNS) == 106
    assert len({tuple(p) for p in linear.C128_PATTERNS}) == 106
    for pattern in linear.C128_PATTERNS:
        assert sum(pattern) == 11
        assert (pattern[0] + pattern[2] + pattern[4]) % 2 == 0  # even bar modules
    assert sum(linear.C128_STOP) == 13


def test_code39_table_has_three_wide_elements():
    for code in linear.C39_ENCODINGS + [linear.C39_ASTERISK]:
        assert bin(code).count("1") == 3


# ---------------------------------------------------------------- symbologies


@pytest.mark.parametrize(
    "text",
    [
        "SHEET-482913",  # code set B
        "0123456789012345",  # code set C
        "AB12345678cd",  # B -> C -> B switching
        "HELLO\tWORLD",  # code set A control character
        "x",
    ],
)
@pytest.mark.parametrize("scale", [1.5, 2.0, 3.0, 4.0])
def test_code128_all_code_sets(text, scale):
    assert decode_text(render(text, "Code128", scale)) == text


@pytest.mark.parametrize(
    "fmt,text",
    [
        ("Code39", "AB-12.3 X"),
        ("ITF", "12345678"),
        ("ITF", "00123456789012"),
        ("EAN13", "590123412345"),
        ("EAN8", "9638507"),
        ("UPCA", "03600029145"),
    ],
)
@pytest.mark.parametrize("scale", [1.5, 2.0, 3.0])
def test_other_symbologies_match_zxing(fmt, text, scale):
    expected = zxing_text(text, fmt)
    assert expected
    assert decode_text(render(text, fmt, scale)) == expected


def test_upca_reported_like_the_installed_zxing():
    image = render("03600029145", "UPCA", 2)
    expected = zxingcpp.read_barcodes(image)[0]
    found = linear.decode(image)
    assert found["text"] == expected.text
    assert found["format"] == barcode.format_label(expected.format)
    restricted = linear.decode(image, ["UPCA"])
    assert restricted["format"] == "UPC-A"
    assert len(restricted["text"]) == 12


def test_code39_check_digit_option():
    # "CODE39" + mod 43 check character "W" (sum of values = 12+24+13+14+3+9 = 75 -> 32 = "W")
    text = "CODE39W"
    image = render(text, "Code39", 2)
    assert decode_text(image) == text
    assert decode_text(image, code39Checksum=True) == "CODE39"
    wrong = render("CODE39X", "Code39", 2)
    assert decode_text(wrong, code39Checksum=True) is None


def test_itf_check_digit_and_min_length():
    image = render("00012345678905", "ITF", 2)  # valid GS1 mod-10 check digit
    assert decode_text(image, itfChecksum=True) == "00012345678905"
    bad = render("00012345678904", "ITF", 2)
    assert decode_text(bad, itfChecksum=True) is None
    assert decode_text(render("1234", "ITF", 2)) is None  # shorter than 6 digits
    assert decode_text(render("1234", "ITF", 2), itfMinLength=4) == "1234"


def test_format_restriction():
    image = render("SHEET-1", "Code128", 2)
    assert decode_text(image, ["Code39"]) is None
    assert decode_text(image, ["Code 128"]) == "SHEET-1"
    assert linear.builtin_formats(["QRCode"]) == set()


def test_bad_checksum_is_rejected():
    """Corrupt one Code 128 data character: the mod-103 checksum must fail."""
    image = render("SHEET-482913", "Code128", 3)
    # Find the 4th symbol's bars on a clean row and repaint them as another symbol
    profile = image[image.shape[0] // 2].astype(float)
    edges, first_dark = linear.line_edges(profile)
    runs = linear.runs_from_edges(edges, first_dark, profile.size)
    assert linear.decode_code128(runs)[0] == "SHEET-482913"
    start = 1 + 6 * 3
    pattern = linear.C128_PATTERNS[40]  # an arbitrary other value
    module = sum(runs[start : start + 6]) / 11
    runs[start : start + 6] = [p * module for p in pattern]
    assert linear.decode_code128(runs) is None


# ---------------------------------------------------------------- robustness


@pytest.mark.parametrize(
    "blur,noise,rotation",
    [(0, 0, 0), (1.0, 0, 0), (0, 20, 0), (0.8, 10, 2), (0, 0, 4), (1.0, 15, 3)],
)
def test_degraded_codes_decode(blur, noise, rotation):
    rng = random.Random(int(blur * 10 + noise + rotation))
    for fmt in ["Code128", "Code39", "ITF", "EAN13"]:
        text = {
            "Code128": "SHEET-%06d" % rng.randint(0, 999999),
            "Code39": "".join(rng.choice(string.ascii_uppercase) for _ in range(6)),
            "ITF": "%010d" % rng.randint(0, 10**10 - 1),
            "EAN13": "%012d" % rng.randint(10**11, 10**12 - 1),
        }[fmt]
        image = degrade(render(text, fmt, 2.5), 1, blur, noise, rotation)
        assert decode_text(image) == zxing_text(text, fmt), (fmt, text)


def test_vertical_and_upside_down_codes():
    image = render("SHEET-000123", "Code128", 2, height=80)
    assert decode_text(np.ascontiguousarray(np.rot90(image))) == "SHEET-000123"
    assert decode_text(np.ascontiguousarray(np.rot90(image, -1))) == "SHEET-000123"
    assert decode_text(np.ascontiguousarray(image[::-1, ::-1])) == "SHEET-000123"


def test_one_scanline_is_not_enough():
    image = render("SHEET-1", "Code128", 2, height=60)
    damaged = image.copy()
    damaged[:, :] = 255
    # Keep only a 3-pixel-high strip of the code: a single scanline can read it
    damaged[48:51] = image[48:51]
    assert linear.decode(damaged, lines=12) is None
    assert decode_text(image) == "SHEET-1"


def _text_image(rng, w=480, h=140):
    image = np.full((h, w), 255, np.uint8)
    for _ in range(rng.randint(1, 4)):
        text = "".join(
            rng.choice(string.ascii_letters + string.digits + " -|/")
            for _ in range(rng.randint(5, 30))
        )
        font = rng.choice(
            [cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_PLAIN, cv2.FONT_HERSHEY_DUPLEX]
        )
        cv2.putText(
            image,
            text,
            (rng.randint(0, 40), rng.randint(20, h - 5)),
            font,
            rng.uniform(0.6, 2.2),
            0,
            rng.randint(1, 3),
        )
    return image


def _noise_image(rng, w=480, h=140):
    g = np.random.default_rng(rng.randint(0, 10**9))
    kind = rng.choice(["gauss", "salt", "stripes", "bubbles"])
    if kind == "gauss":
        return np.clip(g.normal(200, 50, (h, w)), 0, 255).astype(np.uint8)
    if kind == "salt":
        return np.where(g.random((h, w)) < 0.3, 0, 255).astype(np.uint8)
    if kind == "stripes":  # random vertical bars: the hardest negative
        row = np.repeat(np.where(g.random(w // 2) < 0.5, 0, 255), 2)[:w]
        image = np.repeat(row[None, :].astype(np.uint8), h, axis=0)
        return cv2.GaussianBlur(image, (0, 0), rng.uniform(0.3, 1.2))
    image = np.full((h, w), 255, np.uint8)
    for x in range(20, w - 20, 34):
        for y in range(20, h - 10, 34):
            cv2.circle(image, (x, y), 11, 0, 1 if rng.random() < 0.7 else -1)
    return image


def test_no_false_reads_on_text_and_noise():
    rng = random.Random(11)
    for i in range(150):
        image = _text_image(rng) if i % 2 == 0 else _noise_image(rng)
        assert linear.decode(image) is None, i


def test_decoder_is_fast():
    image = render("SHEET-482913", "Code128", 2)
    linear.decode(image)
    started = time.perf_counter()
    for _ in range(10):
        assert linear.decode(image)
    assert (time.perf_counter() - started) / 10 < 0.05  # typically ~2 ms


# ---------------------------------------------------------------- engine chain


def _zone(zone_type="barcode", options=None, size=None, name="code"):
    w, h = size or (600, 140)
    return Zone(name, zone_type, [20, 20], [w, h], dict(options or {}))


def _page(code, size=(700, 220)):
    page = np.full((size[1], size[0]), 255, np.uint8)
    h, w = code.shape[:2]
    page[30 : 30 + h, 30 : 30 + w] = code
    return page


def test_zxing_reads_first_and_is_recorded():
    page = _page(render("SHEET-1", "Code128", 2))
    result = read_zone(_zone(), page)
    assert result.value == "SHEET-1"
    assert result.engine == "zxing"
    assert "decoded_by_fallback" not in result.flags
    assert not result.needs_review


def test_builtin_engine_runs_when_zxing_reads_nothing(monkeypatch):
    monkeypatch.setattr(barcode, "decode_symbols", lambda crop, formats=None: [])
    page = _page(render("SHEET-2", "Code128", 2))
    result = read_zone(_zone(), page)
    assert result.value == "SHEET-2"
    assert result.engine == "builtin"
    assert result.format == "Code 128"
    assert "decoded_by_fallback" in result.flags
    assert not result.needs_review
    assert result.details["engines"] == ["zxing", "builtin"]
    # Opt-in: send fallback decodes to review
    review = read_zone(
        _zone(), page, {"barcode_params": {"review_fallback_decodes": True}}
    )
    assert review.needs_review


def test_builtin_engine_without_zxing_installed(monkeypatch):
    monkeypatch.setattr(barcode, "zxingcpp", None)
    page = _page(render("SHEET-3", "Code128", 2))
    result = read_zone(_zone(), page)
    assert (result.value, result.engine) == ("SHEET-3", "builtin")
    assert result.details["engines"] == ["builtin"]


def test_engine_order_and_unavailable(monkeypatch):
    page = _page(render("SHEET-4", "Code128", 2))
    result = read_zone(_zone(options={"engines": ["builtin", "zxing"]}), page)
    assert result.engine == "builtin"
    monkeypatch.setattr(barcode, "zxingcpp", None)
    nothing = read_zone(_zone(options={"engines": ["zxing"]}), page)
    assert nothing.flags == ["engine_unavailable"]
    blank = read_zone(_zone(), np.full((220, 700), 255, np.uint8))
    assert "not_found" in blank.flags and blank.needs_review


def test_opencv_qr_fallback(monkeypatch):
    monkeypatch.setattr(barcode, "decode_symbols", lambda crop, formats=None: [])
    symbol = zxingcpp.create_barcode("QR-123", zxingcpp.BarcodeFormat.QRCode)
    code = np.array(zxingcpp.write_barcode_to_image(symbol, scale=6), np.uint8)
    code = cv2.copyMakeBorder(code, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    page = _page(code, (code.shape[1] + 60, code.shape[0] + 60))
    zone = _zone("qrcode", size=(code.shape[1], code.shape[0]))
    result = read_zone(zone, page)
    assert (result.value, result.engine, result.format) == (
        "QR-123",
        "opencv",
        "QR Code",
    )
    assert "decoded_by_fallback" in result.flags
    # A Code 128-only barcode zone never asks the QR detector
    assert barcode._read_opencv(_zone(options={"formats": ["Code128"]}), code) is None


def test_pyzbar_is_optional_and_off_by_default(monkeypatch):
    calls = []

    def fake_decode(crop):
        calls.append(crop.shape)
        return [types.SimpleNamespace(data=b"ZBAR-1", type="CODE128")]

    monkeypatch.setattr(barcode, "pyzbar", types.SimpleNamespace(decode=fake_decode))
    monkeypatch.setattr(barcode, "decode_symbols", lambda crop, formats=None: [])
    blank = np.full((220, 700), 255, np.uint8)
    off = read_zone(_zone(), blank)
    assert off.flags == ["not_found"] and not calls
    assert "pyzbar" not in off.details["engines"]

    on = read_zone(_zone(), blank, {"barcode_params": {"pyzbar": True}})
    assert (on.value, on.engine, on.format) == ("ZBAR-1", "pyzbar", "Code 128")
    per_zone = read_zone(_zone(options={"pyzbar": True, "formats": ["EAN13"]}), blank)
    assert per_zone.flags == ["not_found"]  # format filter applies to pyzbar too

    monkeypatch.setattr(barcode, "pyzbar", None)
    missing = read_zone(_zone(), blank, {"barcode_params": {"pyzbar": True}})
    assert "pyzbar" not in missing.details["engines"]


def test_available_engines():
    engines = barcode.available_engines()
    assert engines["builtin"] and engines["opencv"] and engines["zxing"]
