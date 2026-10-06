"""
Built-in 1D barcode decoder: plain numpy, no native dependencies.

It is the second engine in the barcode chain (src/readers/barcode.py) and runs
only when ZXing-C++ reads nothing. Supported symbologies: Code 128 (code sets
A/B/C, FNC1/FNC4, checksum verified), Code 39 (optional mod 43 check digit),
Interleaved 2 of 5 (ITF) and EAN-13 / EAN-8 / UPC-A (check digit verified).

How it works:

1. Scanlines. Several horizontal lines across the zone, each the mean of a few
   pixel rows. A zone taller than it is wide is scanned on its 90-degree
   rotation first. Each line is read left-to-right and right-to-left, so
   upside-down codes decode too. Slight skew is harmless: bars are tall.
2. Binarisation per line. A pixel is dark below the midpoint of the local
   minimum/maximum envelope; lines and stretches without real contrast are
   treated as paper. Edges are placed with sub-pixel interpolation, which keeps
   1-2 px modules usable.
3. Run lengths. Alternating bar/space widths, starting with a bar after a
   quiet zone.
4. Pattern matching. Each character's element widths are normalised by the
   character's own width and matched against the symbology table with a
   tolerance. Start/stop patterns, quiet zones, check digits and module
   consistency are verified.
5. Agreement. A result is accepted only when at least `min_agree` scanlines
   decode the same text, which keeps random text and noise from producing
   reads.

The decode stage uses simple loops over small lists so it can be ported to
JavaScript line by line; only the scanline and edge extraction is vectorised.
"""

from collections import Counter

import numpy as np

CODE128 = "Code 128"
CODE39 = "Code 39"
ITF = "ITF"
EAN13 = "EAN-13"
EAN8 = "EAN-8"
UPCA = "UPC-A"
ALL_FORMATS = (CODE128, CODE39, ITF, EAN13, EAN8, UPCA)

# Template "formats" spellings (zxing names and labels) -> builtin formats
_FORMAT_ALIASES = {
    "code128": CODE128,
    "code39": CODE39,
    "itf": ITF,
    "ean13": EAN13,
    "ean8": EAN8,
    "upca": UPCA,
    "linearcodes": None,  # all of them
    "all": None,
    "any": None,
}


def builtin_formats(names):
    """Map a zone's `formats` list onto the symbologies this decoder reads.

    None means no restriction; an empty set means none of the requested
    formats is supported here.
    """
    if not names:
        return set(ALL_FORMATS)
    wanted = set()
    for name in names:
        key = "".join(ch for ch in str(name).lower() if ch.isalnum())
        if key in _FORMAT_ALIASES:
            alias = _FORMAT_ALIASES[key]
            if alias is None:
                return set(ALL_FORMATS)
            wanted.add(alias)
    return wanted


# --------------------------------------------------------------------------
# Symbology tables
# --------------------------------------------------------------------------

# Code 128: element widths (bar, space, bar, space, bar, space) per value 0..106
_C128_PATTERNS = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232"
).split()
C128_PATTERNS = [[int(c) for c in p] for p in _C128_PATTERNS]
C128_STOP = [2, 3, 3, 1, 1, 1, 2]
C128_START_A, C128_START_B, C128_START_C = 103, 104, 105
C128_FNC1, C128_FNC2, C128_FNC3 = 102, 97, 96
C128_SHIFT, C128_CODE_C, C128_CODE_B, C128_CODE_A = 98, 99, 100, 101

# Code 39: 9 elements (b s b s b s b s b), bit set = wide element, MSB first
C39_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%"
C39_ENCODINGS = [
    0x034, 0x121, 0x061, 0x160, 0x031, 0x130, 0x070, 0x025, 0x124, 0x064,
    0x109, 0x049, 0x148, 0x019, 0x118, 0x058, 0x00D, 0x10C, 0x04C, 0x01C,
    0x103, 0x043, 0x142, 0x013, 0x112, 0x052, 0x007, 0x106, 0x046, 0x016,
    0x181, 0x0C1, 0x1C0, 0x091, 0x190, 0x0D0, 0x085, 0x184, 0x0C4, 0x0A8,
    0x0A2, 0x08A, 0x02A,
]  # fmt: skip
C39_ASTERISK = 0x094
C39_BY_CODE = {code: C39_ALPHABET[i] for i, code in enumerate(C39_ENCODINGS)}
C39_BY_CODE[C39_ASTERISK] = "*"

# ITF: 5 elements per digit, 1 = wide
ITF_PATTERNS = [
    [1, 1, 2, 2, 1],
    [2, 1, 1, 1, 2],
    [1, 2, 1, 1, 2],
    [2, 2, 1, 1, 1],
    [1, 1, 2, 1, 2],
    [2, 1, 2, 1, 1],
    [1, 2, 2, 1, 1],
    [1, 1, 1, 2, 2],
    [2, 1, 1, 2, 1],
    [1, 2, 1, 2, 1],
]

# EAN/UPC: L-code element widths (space, bar, space, bar); R-codes are the same
# widths starting with a bar, G-codes are the L-codes reversed
EAN_L = [
    [3, 2, 1, 1],
    [2, 2, 2, 1],
    [2, 1, 2, 2],
    [1, 4, 1, 1],
    [1, 1, 3, 2],
    [1, 2, 3, 1],
    [1, 1, 1, 4],
    [1, 3, 1, 2],
    [1, 2, 1, 3],
    [3, 1, 1, 2],
]
EAN_G = [list(reversed(p)) for p in EAN_L]
# Parity of the six left digits (0 = L, 1 = G) encodes the first EAN-13 digit
EAN_FIRST_DIGIT = {
    (0, 0, 0, 0, 0, 0): 0,
    (0, 0, 1, 0, 1, 1): 1,
    (0, 0, 1, 1, 0, 1): 2,
    (0, 0, 1, 1, 1, 0): 3,
    (0, 1, 0, 0, 1, 1): 4,
    (0, 1, 1, 0, 0, 1): 5,
    (0, 1, 1, 1, 0, 0): 6,
    (0, 1, 0, 1, 0, 1): 7,
    (0, 1, 0, 1, 1, 0): 8,
    (0, 1, 1, 0, 1, 0): 9,
}

# zxing-cpp >= 2.3 reports a UPC-A as a 13-digit EAN-13 unless only UPC-A is
# allowed; 2.2 (the Windows 7 build) reports 12 digits as UPC-A. barcode.py
# sets this to match the installed zxing-cpp so both engines agree.
UPCA_AS_EAN13 = True

# Max mean absolute deviation (in modules) of a character's elements from the
# matched pattern, and the minimum gap to the runner-up pattern
MAX_PATTERN_ERROR = 0.32
MIN_PATTERN_MARGIN = 0.12


# --------------------------------------------------------------------------
# Scanlines and run lengths (vectorised)
# --------------------------------------------------------------------------


def _box_filter(values, radius):
    """Moving average with edge replication (length preserved)."""
    if radius < 1:
        return values
    padded = np.concatenate(
        [np.full(radius, values[0]), values, np.full(radius, values[-1])]
    )
    csum = np.concatenate([[0.0], np.cumsum(padded)])
    width = 2 * radius + 1
    return (csum[width:] - csum[:-width]) / width


def _moving_extreme(values, radius, func):
    """Moving max/min over a window, via a strided view (no Python loop)."""
    padded = np.concatenate(
        [np.full(radius, values[0]), values, np.full(radius, values[-1])]
    )
    shape = (values.size, 2 * radius + 1)
    strides = (padded.strides[0], padded.strides[0])
    windows = np.lib.stride_tricks.as_strided(padded, shape=shape, strides=strides)
    return func(windows, axis=1)


def scanline_profiles(gray, count=12, band=3):
    """Mean intensity profiles of `count` horizontal bands across the image."""
    h = gray.shape[0]
    top, bottom = int(h * 0.08), max(int(h * 0.92), int(h * 0.08) + 1)
    centres = np.unique(np.linspace(top, bottom - 1, count).astype(int))
    rows = []
    for c in centres:
        y0, y1 = max(c - band // 2, 0), min(c + band // 2 + 1, h)
        rows.append(gray[y0:y1].mean(axis=0))
    return rows


def line_edges(profile, min_contrast=40):
    """
    Sub-pixel bar/space boundaries of one scanline.

    Returns (edges, first_is_dark) where edges are fractional x positions of
    every dark/light transition, or None when the line has no usable contrast.
    """
    line = np.asarray(profile, dtype=np.float64)
    n = line.size
    if n < 20:
        return None
    lo, hi = np.percentile(line, 3), np.percentile(line, 97)
    if hi - lo < min_contrast:
        return None
    # Local envelope: the window spans several bars, so it sees both a bar and
    # a space wherever there is a code
    radius = max(int(n / 24), 6)
    env_max = _box_filter(_moving_extreme(line, radius, np.max), radius // 2)
    env_min = _box_filter(_moving_extreme(line, radius, np.min), radius // 2)
    threshold = (env_max + env_min) / 2.0
    # Where the local contrast is weak (quiet zones, paper), fall back to the
    # global midpoint so paper noise does not become bars
    weak = (env_max - env_min) < (hi - lo) * 0.35
    threshold = np.where(weak, (hi + lo) / 2.0, threshold)
    dark = line < threshold
    change = np.nonzero(dark[1:] != dark[:-1])[0]
    if change.size < 6:
        return None
    # Linear interpolation of the crossing between pixel i and i+1
    d = line - threshold
    a, b = d[change], d[change + 1]
    denom = np.where(a == b, 1.0, a - b)
    frac = np.clip(a / denom, 0.0, 1.0)
    edges = change + frac + 0.5
    return edges, bool(dark[0])


def runs_from_edges(edges, first_is_dark, length):
    """
    Run widths as a list starting with the leading space (quiet zone) before
    the first bar. widths[1], widths[3], ... are bars.
    """
    bounds = np.concatenate([[0.0], edges, [float(length)]])
    widths = np.diff(bounds)
    if first_is_dark:
        # The image starts inside a bar: give it a zero-width leading space
        widths = np.concatenate([[0.0], widths])
    return [float(w) for w in widths]


# --------------------------------------------------------------------------
# Pattern matching helpers (plain loops: easy to port)
# --------------------------------------------------------------------------


def _match(widths, patterns, total_modules):
    """Best pattern index for the given element widths, or -1."""
    total = 0.0
    for w in widths:
        total += w
    if total <= 0:
        return -1
    module = total / total_modules
    best, best_err, second_err = -1, 1e9, 1e9
    for index, pattern in enumerate(patterns):
        err = 0.0
        for w, p in zip(widths, pattern):
            err += abs(w / module - p)
        err /= len(pattern)
        if err < best_err:
            best, second_err, best_err = index, best_err, err
        elif err < second_err:
            second_err = err
    if best_err > MAX_PATTERN_ERROR or second_err - best_err < MIN_PATTERN_MARGIN:
        return -1
    return best


def _quiet_enough(space, module, modules_required, at_border):
    return at_border or space >= module * modules_required


def _ok_ratio(a, b, tolerance=0.35):
    """True when two module estimates agree within the relative tolerance."""
    return abs(a - b) <= tolerance * max(a, b)


def _quiet_before(runs, i, element_count, total_modules, modules_required):
    """Cheap pre-check (before any fitting) that a quiet zone precedes bar i."""
    total = 0.0
    for k in range(i, i + element_count):
        total += runs[k]
    module = total / total_modules
    if i == 1 and runs[0] >= 2 * module:
        return True  # the quiet zone runs into the crop border
    return runs[i - 1] >= module * modules_required * 0.7


def _sign(k, first_is_bar):
    """+1 for a bar, -1 for a space, for element k of a character."""
    return 1.0 if (k % 2 == 0) == first_is_bar else -1.0


def _fit(widths, pattern, first_is_bar):
    """
    Least-squares fit of widths = module * pattern + sign * bias.

    `bias` is the ink spread: blur and print gain widen bars and narrow spaces
    by the same amount, so estimating it once per character makes narrow
    elements readable on blurry scans. Returns (module, bias, error) with the
    error as the mean residual in modules.
    """
    spp = sps = sss = swp = sws = 0.0
    for k in range(len(pattern)):
        p, s, w = pattern[k], _sign(k, first_is_bar), widths[k]
        spp += p * p
        sps += p * s
        sss += s * s
        swp += w * p
        sws += w * s
    det = spp * sss - sps * sps
    if det <= 0:
        return 0.0, 0.0, 1e9
    module = (swp * sss - sws * sps) / det
    bias = (spp * sws - sps * swp) / det
    if module <= 0:
        return 0.0, 0.0, 1e9
    # More spread than half a module means the elements are not resolved
    limit = 0.45 * module
    bias = max(-limit, min(limit, bias))
    err = 0.0
    for k in range(len(pattern)):
        err += abs(widths[k] - module * pattern[k] - _sign(k, first_is_bar) * bias)
    return module, bias, err / (len(pattern) * module)


def _match_fit(widths, patterns, first_is_bar):
    """(index, module, bias) of the best-fitting pattern, or (-1, 0, 0)."""
    best, best_err, second_err, fit = -1, 1e9, 1e9, (0.0, 0.0)
    for index, pattern in enumerate(patterns):
        module, bias, err = _fit(widths, pattern, first_is_bar)
        if err < best_err:
            best, second_err, best_err, fit = index, best_err, err, (module, bias)
        elif err < second_err:
            second_err = err
    if best_err > MAX_PATTERN_ERROR or second_err - best_err < MIN_PATTERN_MARGIN:
        return -1, 0.0, 0.0
    return best, fit[0], fit[1]


def _debias(widths, bias, first_is_bar):
    return [
        max(widths[k] - _sign(k, first_is_bar) * bias, 0.05) for k in range(len(widths))
    ]


def _update_bias(bias, widths, pattern, first_is_bar):
    _, measured, err = _fit(widths, pattern, first_is_bar)
    if err > MAX_PATTERN_ERROR:
        return bias
    return 0.7 * bias + 0.3 * measured


# --------------------------------------------------------------------------
# Code 128
# --------------------------------------------------------------------------


def decode_code128(runs):
    """Try every bar after a quiet zone as the start character."""
    n = len(runs)
    for i in range(1, n - 6 * 3 - 7, 2):
        if not _quiet_before(runs, i, 6, 11, 5):
            continue
        start, module, bias = _match_fit(runs[i : i + 6], C128_PATTERNS[103:106], True)
        if start < 0:
            continue
        if not _quiet_enough(runs[i - 1], module, 5, i == 1 and runs[0] >= 2 * module):
            continue
        result = _code128_from(runs, i, 103 + start, module, bias)
        if result is not None:
            return result
    return None


def _code128_from(runs, i, start_value, module, bias):
    values = [start_value]
    pos = i + 6
    n = len(runs)
    while pos + 7 <= n:
        # Stop pattern (7 elements, 13 modules) followed by a quiet zone
        stop_widths = _debias(runs[pos : pos + 7], bias, True)
        stop_module = sum(stop_widths) / 13.0
        if _ok_ratio(stop_module, module) and _match(stop_widths, [C128_STOP], 13) == 0:
            trailing = runs[pos + 7] if pos + 7 < n else module * 10
            at_border = pos + 8 >= n
            if _quiet_enough(trailing, module, 5, at_border) and len(values) >= 3:
                return _code128_text(values)
        raw = runs[pos : pos + 6]
        char_widths = _debias(raw, bias, True)
        char_module = sum(char_widths) / 11.0
        if not _ok_ratio(char_module, module):
            return None
        value = _match(char_widths, C128_PATTERNS[:106], 11)
        if value < 0 or value >= 103:
            return None
        values.append(value)
        bias = _update_bias(bias, raw, C128_PATTERNS[value], True)
        module = 0.8 * module + 0.2 * char_module
        pos += 6
    return None


def _code128_text(values):
    """values = [start, data..., checksum]. Returns text or None."""
    checksum = values[0]
    for weight, value in enumerate(values[1:-1], start=1):
        checksum += weight * value
    if checksum % 103 != values[-1]:
        return None
    code_set = {103: "A", 104: "B", 105: "C"}[values[0]]
    out = []
    shift = False
    fnc4_next, fnc4_latched = False, False
    data = values[1:-1]
    for index, value in enumerate(data):
        current = code_set
        if shift:
            current = "B" if code_set == "A" else "A"
            shift = False
        if current == "C":
            if value < 100:
                out.append(f"{value:02d}")
            elif value == C128_CODE_B:
                code_set = "B"
            elif value == C128_CODE_A:
                code_set = "A"
            elif value == C128_FNC1:
                if index > 0:
                    out.append("\x1d")
            continue
        if value < 96:
            if current == "A":
                code = value + 32 if value < 64 else value - 64
            else:
                code = value + 32
            if fnc4_next or fnc4_latched:
                code += 128
                fnc4_next = False
            out.append(chr(code))
            continue
        if value == C128_FNC1:
            if index > 0:
                out.append("\x1d")
        elif value == C128_SHIFT:
            shift = True
        elif value == C128_CODE_C:
            code_set = "C"
        elif (current == "A" and value == C128_CODE_A) or (
            current == "B" and value == C128_CODE_B
        ):
            # FNC4: next character (or, twice in a row, all following) +128
            if fnc4_next:
                fnc4_latched, fnc4_next = not fnc4_latched, False
            else:
                fnc4_next = True
        elif value == C128_CODE_A:
            code_set = "A"
        elif value == C128_CODE_B:
            code_set = "B"
        # FNC2/FNC3 carry no data
    text = "".join(out)
    return (text, CODE128) if text else None


# --------------------------------------------------------------------------
# Code 39
# --------------------------------------------------------------------------


def _code39_char(widths, bias=0.0):
    """(character, narrow, wide, bias) for 9 elements (bar first), or None."""
    widths = _debias(widths, bias, True)
    ordered = sorted(widths)
    narrow_max, wide_min = ordered[5], ordered[6]
    if wide_min < narrow_max * 1.5:
        return None
    narrow = sum(ordered[:6]) / 6.0
    wide = sum(ordered[6:]) / 3.0
    if ordered[0] < narrow * 0.4 or ordered[8] > wide * 1.6:
        return None
    threshold = (narrow_max + wide_min) / 2.0
    code = 0
    narrow_bars, narrow_spaces = [], []
    for k, w in enumerate(widths):
        is_wide = w > threshold
        code = (code << 1) | (1 if is_wide else 0)
        if not is_wide:
            (narrow_bars if k % 2 == 0 else narrow_spaces).append(w)
    char = C39_BY_CODE.get(code)
    if char is None:
        return None
    # Remaining ink spread: narrow bars vs narrow spaces should be equal
    spread = 0.0
    if narrow_bars and narrow_spaces:
        spread = (
            sum(narrow_bars) / len(narrow_bars)
            - sum(narrow_spaces) / len(narrow_spaces)
        ) / 2.0
    return char, narrow, wide, bias + spread


def decode_code39(runs, check_digit=False, extended="auto"):
    n = len(runs)
    for i in range(1, n - 9 * 3, 2):
        if not _quiet_before(runs, i, 9, 15, 7):
            continue
        first = _code39_char(runs[i : i + 9])
        if first is None or first[0] != "*":
            continue
        _, narrow, wide, bias = first
        if not _quiet_enough(runs[i - 1], narrow, 7, i == 1 and runs[0] >= 2 * narrow):
            continue
        result = _code39_from(runs, i + 9, narrow, wide, bias, check_digit, extended)
        if result is not None:
            return result
    return None


def _code39_from(runs, pos, narrow, wide, bias, check_digit, extended):
    chars = []
    n = len(runs)
    while pos + 10 <= n:
        gap = runs[pos] + bias
        # The inter-character gap is a narrow space (tolerate up to 3 narrows)
        if gap < narrow * 0.4 or gap > narrow * 3.2:
            return None
        decoded = _code39_char(runs[pos + 1 : pos + 10], bias)
        if decoded is None:
            return None
        char, char_narrow, char_wide, char_bias = decoded
        if not (_ok_ratio(char_narrow, narrow, 0.45) and _ok_ratio(char_wide, wide)):
            return None
        bias = 0.7 * bias + 0.3 * char_bias
        pos += 10
        if char == "*":
            trailing = runs[pos] if pos < n else narrow * 10
            if not _quiet_enough(trailing, narrow, 7, pos + 1 >= n):
                return None
            if not chars:
                return None
            text = "".join(chars)
            if check_digit:
                if len(text) < 2:
                    return None
                total = sum(C39_ALPHABET.index(c) for c in text[:-1])
                if C39_ALPHABET[total % 43] != text[-1]:
                    return None
                text = text[:-1]
            if extended:
                full = _code39_full_ascii(text)
                if full is not None:
                    text = full
                elif extended is True:
                    return None
            return text, CODE39
        chars.append(char)
    return None


def _code39_full_ascii(text):
    """Full ASCII Code 39 ($A = SOH, +a = a, %U = NUL, /A = !, ...); None if invalid."""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "$%/+":
            if i + 1 >= len(text):
                return None
            nxt = text[i + 1]
            i += 2
            if ch == "+" and "A" <= nxt <= "Z":
                out.append(chr(ord(nxt) + 32))
            elif ch == "$" and "A" <= nxt <= "Z":
                out.append(chr(ord(nxt) - 64))
            elif ch == "%" and "A" <= nxt <= "E":
                out.append(chr(ord(nxt) - 38))
            elif ch == "%" and "F" <= nxt <= "J":
                out.append(chr(ord(nxt) - 11))
            elif ch == "%" and "K" <= nxt <= "O":
                out.append(chr(ord(nxt) + 16))
            elif ch == "%" and "P" <= nxt <= "T":
                out.append(chr(ord(nxt) + 43))
            elif ch == "%" and nxt == "U":
                out.append("\x00")
            elif ch == "%" and nxt == "V":
                out.append("@")
            elif ch == "%" and nxt == "W":
                out.append("`")
            elif ch == "%" and nxt in "XYZ":
                out.append(chr(127))
            elif ch == "/" and "A" <= nxt <= "O":
                out.append(chr(ord(nxt) - 32))
            elif ch == "/" and nxt == "Z":
                out.append(":")
            else:
                return None
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# Interleaved 2 of 5
# --------------------------------------------------------------------------


def decode_itf(runs, min_length=6, check_digit=False):
    n = len(runs)
    start_pattern = [1, 1, 1, 1]
    for i in range(1, n - 4 - 10 - 3, 2):
        if not _quiet_before(runs, i, 4, 4, 8):
            continue
        narrow, bias, err = _fit(runs[i : i + 4], start_pattern, True)
        if err > MAX_PATTERN_ERROR or narrow <= 0:
            continue
        if not _quiet_enough(runs[i - 1], narrow, 8, i == 1 and runs[0] >= 2 * narrow):
            continue
        result = _itf_from(runs, i + 4, narrow, bias, min_length, check_digit)
        if result is not None:
            return result
    return None


def _itf_from(runs, pos, narrow, bias, min_length, check_digit):
    digits = []
    n = len(runs)
    while pos + 3 <= n:
        # Stop: wide bar, narrow space, narrow bar, then quiet zone
        stop = _debias(runs[pos : pos + 3], bias, True)
        if (
            len(digits) >= min_length
            and stop[0] > narrow * 1.7
            and stop[1] < narrow * 1.6
            and stop[2] < narrow * 1.6
        ):
            trailing = runs[pos + 3] if pos + 3 < n else narrow * 10
            if _quiet_enough(trailing, narrow, 8, pos + 4 >= n):
                text = "".join(str(d) for d in digits)
                if check_digit and not _mod10_ok(text):
                    return None
                return text, ITF
        if pos + 10 > n:
            break
        raw = runs[pos : pos + 10]
        block = _debias(raw, bias, True)
        pair_module = sum(block) / 14.0
        if not _ok_ratio(pair_module, narrow, 0.45):
            return None
        first = _match(block[0::2], ITF_PATTERNS, 7)
        second = _match(block[1::2], ITF_PATTERNS, 7)
        if first < 0 or second < 0:
            return None
        digits += [first, second]
        pattern = []
        for k in range(5):
            pattern += [ITF_PATTERNS[first][k], ITF_PATTERNS[second][k]]
        bias = _update_bias(bias, raw, pattern, True)
        narrow = 0.8 * narrow + 0.2 * pair_module
        pos += 10
    return None


def _mod10_ok(text):
    """GS1 mod 10: weights 3,1,3,... from the right, excluding the check digit."""
    if len(text) < 2 or not text.isdigit():
        return False
    total = 0
    for offset, ch in enumerate(reversed(text[:-1])):
        total += int(ch) * (3 if offset % 2 == 0 else 1)
    return (10 - total % 10) % 10 == int(text[-1])


# --------------------------------------------------------------------------
# EAN-13 / EAN-8 / UPC-A
# --------------------------------------------------------------------------


def decode_ean(runs, wanted):
    """Try EAN-13 (incl. UPC-A) then EAN-8 at every bar following a quiet zone."""
    n = len(runs)
    guard = [1, 1, 1]
    for i in range(1, n - 3, 2):
        if not _quiet_before(runs, i, 3, 3, 5):
            continue
        module, bias, err = _fit(runs[i : i + 3], guard, True)
        if err > MAX_PATTERN_ERROR or module <= 0:
            continue
        if not _quiet_enough(runs[i - 1], module, 5, i == 1 and runs[0] >= 2 * module):
            continue
        for digits in (6, 4):
            if digits == 6 and not ({EAN13, UPCA} & wanted):
                continue
            if digits == 4 and EAN8 not in wanted:
                continue
            result = _ean_from(runs, i + 3, module, bias, digits, wanted)
            if result is not None:
                return result
    return None


def _ean_side(runs, pos, module, bias, count, left):
    # Left digits start with a space (L/G codes), right digits with a bar
    first_is_bar = not left
    candidates = EAN_L + EAN_G if left else EAN_L
    digits, parities = [], []
    for _ in range(count):
        raw = runs[pos : pos + 4]
        if len(raw) < 4:
            return None
        widths = _debias(raw, bias, first_is_bar)
        digit_module = sum(widths) / 7.0
        if not _ok_ratio(digit_module, module, 0.3):
            return None
        index = _match(widths, candidates, 7)
        if index < 0:
            return None
        digits.append(index % 10)
        parities.append(index // 10)
        bias = _update_bias(bias, raw, candidates[index], first_is_bar)
        module = 0.8 * module + 0.2 * digit_module
        pos += 4
    return digits, parities, pos, module, bias


def _ean_from(runs, pos, module, bias, half, wanted):
    n = len(runs)
    needed = half * 4 * 2 + 5 + 3
    if pos + needed > n:
        return None
    left = _ean_side(runs, pos, module, bias, half, True)
    if left is None:
        return None
    left_digits, parities, pos, module, bias = left
    _, _, err = _fit(runs[pos : pos + 5], [1, 1, 1, 1, 1], False)
    if err > MAX_PATTERN_ERROR:
        return None
    right = _ean_side(runs, pos + 5, module, bias, half, False)
    if right is None:
        return None
    right_digits, _, pos, module, bias = right
    end_guard = runs[pos : pos + 3]
    if len(end_guard) < 3 or _fit(end_guard, [1, 1, 1], True)[2] > MAX_PATTERN_ERROR:
        return None
    trailing = runs[pos + 3] if pos + 3 < n else module * 10
    if not _quiet_enough(trailing, module, 5, pos + 4 >= n):
        return None
    if half == 6:
        first = EAN_FIRST_DIGIT.get(tuple(parities))
        if first is None:
            return None
        text = str(first) + "".join(str(d) for d in left_digits + right_digits)
        if not _mod10_ok(text):
            return None
        if text[0] == "0" and UPCA in wanted:
            if EAN13 not in wanted or not UPCA_AS_EAN13:
                return text[1:], UPCA
        if EAN13 in wanted:
            return text, EAN13
        return None
    if any(parities):
        return None
    text = "".join(str(d) for d in left_digits + right_digits)
    if not _mod10_ok(text):
        return None
    return text, EAN8


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def decode_runs(runs, wanted, options=None):
    options = options or {}
    if CODE128 in wanted:
        found = decode_code128(runs)
        if found:
            return found
    if {EAN13, EAN8, UPCA} & wanted:
        found = decode_ean(runs, wanted)
        if found:
            return found
    if CODE39 in wanted:
        found = decode_code39(
            runs,
            options.get("code39Checksum", False),
            options.get("code39Extended", "auto"),
        )
        if found:
            return found
    if ITF in wanted:
        found = decode_itf(
            runs,
            min_length=options.get("itfMinLength", 6),
            check_digit=options.get("itfChecksum", False),
        )
        if found:
            return found
    return None


def sharpen(profile, amount=1.5, radius=1):
    """1-D unsharp mask: restores the contrast of narrow elements after blur."""
    smooth = _box_filter(_box_filter(profile, radius), radius)
    return profile + amount * (profile - smooth)


def _decode_line(profile, width, wanted, options):
    edges = line_edges(profile)
    if edges is None:
        return None
    runs = runs_from_edges(edges[0], edges[1], width)
    found = decode_runs(runs, wanted, options)
    if found is None:
        # Right-to-left (upside-down code); keep "leading space first"
        reverse = runs[::-1]
        if len(runs) % 2 == 0:  # the line ended inside a bar
            reverse = [0.0] + reverse
        found = decode_runs(reverse, wanted, options)
    return found


def _scan(gray, wanted, options, min_agree, lines):
    votes = Counter()
    width = gray.shape[1]
    for profile in scanline_profiles(gray, count=lines):
        found = _decode_line(profile, width, wanted, options)
        if found is None:
            found = _decode_line(sharpen(profile), width, wanted, options)
        if found is None:
            found = _decode_line(sharpen(profile, 3.0, 2), width, wanted, options)
        if found is not None:
            votes[found] += 1
            if votes[found] >= min_agree and votes[found] * 2 > sum(votes.values()):
                return found, votes[found]
    if votes:
        found, count = votes.most_common(1)[0]
        if count >= min_agree:
            return found, count
    return None


def decode(gray, formats=None, options=None, min_agree=2, lines=12):
    """
    Decode one linear barcode in a grayscale crop.

    `formats`: iterable of template format names (None = all supported).
    Returns {"text", "format", "votes", "rotated"} or None.
    """
    wanted = builtin_formats(formats)
    if not wanted:
        return None
    gray = np.asarray(gray)
    if gray.ndim == 3:
        gray = gray.mean(axis=2)
    gray = gray.astype(np.float32)
    h, w = gray.shape[:2]
    orientations = [False, True] if w >= h else [True, False]
    if max(w, h) > 2 * min(w, h):
        # A clearly wide (or tall) zone holds its code along the long side
        orientations = orientations[:1]
    for rotated in orientations:
        view = np.ascontiguousarray(np.rot90(gray)) if rotated else gray
        found = _scan(view, wanted, options or {}, min_agree, lines)
        if found is not None:
            (text, fmt), votes = found
            return {"text": text, "format": fmt, "votes": votes, "rotated": rotated}
    return None
