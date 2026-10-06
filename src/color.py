"""
Colour handling: colour dropout (turning a colour sheet into the grey image the
reader works on) and a palette tool that finds a sheet's main colours and
suggests dropout settings.

Template key (all optional; absent = plain grey conversion, today's behaviour):

    "colorDropout": {"mode": "red", "strength": 1.0}
    "colorDropout": {"mode": "color", "color": "#E8618C", "tolerance": 60}

Modes:
    grey   plain luminance (default)
    red    the red channel: red/pink/orange print turns light, blue/black stay dark
    green  the green channel
    blue   the blue channel: blue/cyan print turns light
    max    per-pixel max of B, G, R: any saturated colour turns light, black and
           pencil stay dark (blue pen turns light too)
    color  pixels within `tolerance` (CIE Lab distance, lightness half weighted)
           of `color` are pushed to white, with a soft falloff up to 1.5x tolerance

strength (0..1) blends between plain grey (0) and the dropout result (1).
"""

from collections import namedtuple
from functools import lru_cache

import cv2
import numpy as np

DROPOUT_MODES = ("grey", "red", "green", "blue", "max", "color")
CHANNEL_INDEX = {"blue": 0, "green": 1, "red": 2}
DEFAULT_TOLERANCE = 60.0
# Lightness counts half as much as hue/chroma: a print colour scanned lighter or
# darker is still the same ink, while a dark pen stroke over it is not
LIGHTNESS_WEIGHT = 0.5
FALLOFF = 1.5

DropoutSpec = namedtuple("DropoutSpec", "mode color tolerance strength")
GREY = DropoutSpec("grey", None, 0.0, 1.0)

# Marks a reader must keep dark: black/blue pens and pencil
DEFAULT_KEEP_COLORS = ["#202020", "#6E6E6E", "#1F3C9A", "#2A3F7A"]


# --------------------------------------------------------------------------- specs


def parse_hex(value):
    """'#E8618C' / 'E8618C' / '#e86' -> (b, g, r) ints."""
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        raise ValueError(f"Not a hex colour: {value!r}")
    r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    return (b, g, r)


def to_hex(bgr):
    b, g, r = (int(round(float(v))) for v in bgr[:3])
    return f"#{r:02X}{g:02X}{b:02X}"


def normalize_dropout(spec):
    """Canonical, hashable DropoutSpec from a template value (dict, mode string or None)."""
    if spec is None:
        return GREY
    if isinstance(spec, DropoutSpec):
        return spec
    if isinstance(spec, str):
        spec = {"mode": spec}
    mode = str(spec.get("mode", "grey")).lower()
    if mode == "gray":
        mode = "grey"
    if mode not in DROPOUT_MODES:
        raise ValueError(f"Unknown colorDropout mode {mode!r}")
    strength = float(np.clip(float(spec.get("strength", 1.0)), 0.0, 1.0))
    if mode == "grey" or strength <= 0:
        return GREY
    color, tolerance = None, 0.0
    if mode == "color":
        if not spec.get("color"):
            raise ValueError("colorDropout mode 'color' needs a 'color' (#RRGGBB)")
        color = parse_hex(spec["color"])
        tolerance = float(spec.get("tolerance", DEFAULT_TOLERANCE))
        if tolerance <= 0:
            return GREY
    return DropoutSpec(mode, color, tolerance, strength)


def dropout_to_json(spec):
    spec = normalize_dropout(spec)
    out = {"mode": spec.mode}
    if spec.mode == "color":
        out["color"] = to_hex(spec.color)
        out["tolerance"] = spec.tolerance
    if spec.mode != "grey":
        out["strength"] = spec.strength
    return out


def is_grey(spec):
    return normalize_dropout(spec).mode == "grey"


# --------------------------------------------------------------------------- dropout


def to_bgr(image):
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return image


def apply_dropout(image, spec=None):
    """
    Convert a BGR (or BGRA) sheet image to the grey image the reader uses.

    A grey (2-D) image is returned unchanged: there is no colour left to drop.
    """
    if image is None or image.ndim == 2:
        return image
    spec = normalize_dropout(spec)
    image = to_bgr(image)
    grey = None
    if spec.mode in ("grey", "color") or spec.strength < 1.0:
        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if spec.mode == "grey":
        return grey
    if spec.mode in CHANNEL_INDEX:
        dropped = cv2.extractChannel(image, CHANNEL_INDEX[spec.mode])
    elif spec.mode == "max":
        b, g, r = cv2.split(image)
        dropped = cv2.max(cv2.max(b, g), r)
    else:
        weight = color_match_weight(image, spec.color, spec.tolerance)
        # Push matching pixels to white: grey + w * (255 - grey)
        lift = cv2.multiply(cv2.bitwise_not(grey), weight, scale=1.0 / 255)
        dropped = cv2.add(grey, lift)
    if spec.strength < 1.0:
        dropped = cv2.addWeighted(grey, 1.0 - spec.strength, dropped, spec.strength, 0)
    return dropped


def color_match_weight(image, color_bgr, tolerance):
    """
    Per-pixel weight as uint8 (255 = fully matching): 255 within tolerance of the
    colour, falling to 0 at 1.5x tolerance.

    Looks the weight up in a 32x32x32 colour table (built once per setting), which
    is several times faster than a per-pixel Lab distance on a full page.
    """
    table = _weight_table(tuple(int(c) for c in color_bgr), float(tolerance))
    b, g, r = cv2.split(image)
    index = cv2.bitwise_or(
        cv2.bitwise_or(cv2.LUT(b, _SHIFT_B), cv2.LUT(g, _SHIFT_G)),
        cv2.LUT(r, _SHIFT_R),
    )
    return np.take(table, index)


_LEVELS = np.arange(256)
_SHIFT_B = ((_LEVELS >> 3) << 10).astype(np.uint16)
_SHIFT_G = ((_LEVELS >> 3) << 5).astype(np.uint16)
_SHIFT_R = (_LEVELS >> 3).astype(np.uint16)


@lru_cache(maxsize=16)
def _weight_table(color_bgr, tolerance):
    centres = np.arange(32, dtype=np.float32) * 8 + 4
    b, g, r = np.meshgrid(centres, centres, centres, indexing="ij")
    cube = np.stack([b, g, r], axis=-1).reshape(-1, 1, 3) / 255.0
    lab = cv2.cvtColor(cube.astype(np.float32), cv2.COLOR_BGR2Lab).reshape(-1, 3)
    target = _true_lab([color_bgr])[0]
    delta = lab - target
    delta[:, 0] *= LIGHTNESS_WEIGHT
    distance = np.sqrt(np.sum(delta * delta, axis=1))
    falloff = (FALLOFF - 1.0) * tolerance
    weight = np.clip((FALLOFF * tolerance - distance) / falloff, 0.0, 1.0)
    return np.round(weight * 255).astype(np.uint8)


def lab_distance(bgr_a, bgr_b):
    """Distance used by the 'color' mode between two BGR colours."""
    lab = _true_lab([bgr_a, bgr_b])
    delta = lab[0] - lab[1]
    delta[0] *= LIGHTNESS_WEIGHT
    return float(np.sqrt(np.sum(delta * delta)))


# --------------------------------------------------------------------------- loading


def decode_for(image_bytes, color):
    """Decode an encoded image, in colour only when a dropout needs it."""
    flag = cv2.IMREAD_COLOR if color else cv2.IMREAD_GRAYSCALE
    return cv2.imdecode(np.frombuffer(image_bytes, np.uint8), flag)


# --------------------------------------------------------------------------- palette


def _true_lab(bgr_pixels):
    """(N, 3) uint8 BGR -> (N, 3) float32 Lab with L in 0..100."""
    pixels = np.asarray(bgr_pixels, dtype=np.float32).reshape(-1, 1, 3) / 255.0
    return cv2.cvtColor(pixels, cv2.COLOR_BGR2Lab).reshape(-1, 3)


def label_guess(bgr):
    """A human name for a colour, e.g. 'paper', 'black ink', 'pencil / grey', 'pink'."""
    lightness, a, b = _true_lab([bgr])[0]
    chroma = float(np.hypot(a, b))
    if lightness > 85 and chroma < 12:
        return "paper"
    if chroma < 10:
        if lightness < 35:
            return "black ink"
        if lightness < 72:
            return "pencil / grey"
        return "light grey"
    hue = float(np.degrees(np.arctan2(b, a))) % 360
    if hue < 40 or hue >= 345:
        name = "pink" if lightness > 55 else "red"
    elif hue < 70:
        name = "orange"
    elif hue < 105:
        name = "yellow"
    elif hue < 175:
        name = "green"
    elif hue < 235:
        name = "cyan"
    elif hue < 300:
        name = "blue"
    else:
        name = "purple"
    return name


def extract_palette(image, k=6, max_side=240, ignore_paper=True, min_share=0.002):
    """
    Main colours of a sheet image.

    Returns {"colors": [{hex, share, label_guess, lab}], "paper_share": float}.
    Shares are fractions of all pixels. Near-white paper is reported separately
    (paper_share) and left out of the list unless ignore_paper is False.
    """
    bgr = to_bgr(image)
    h, w = bgr.shape[:2]
    scale = min(1.0, float(max_side) / max(h, w))
    if scale < 1.0:
        bgr = cv2.resize(
            bgr, (max(1, int(w * scale)), max(1, int(h * scale))), cv2.INTER_AREA
        )
    pixels = bgr.reshape(-1, 3)
    lab = _true_lab(pixels)
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    paper = (lab[:, 0] > 85) & (chroma < 12)
    total = float(len(pixels))
    colors = []
    rest = ~paper
    if ignore_paper is False and paper.any():
        colors.append(_palette_entry(pixels[paper], total))
    if rest.sum() >= 1:
        sample_lab = lab[rest].astype(np.float32)
        sample_bgr = pixels[rest]
        clusters = int(min(k, len(sample_lab)))
        cv2.setRNGSeed(12345)
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
        # Lightness counts less so a print colour and its lighter anti-aliasing
        # fall into one cluster, while hue differences separate inks
        weighted = sample_lab * np.float32([LIGHTNESS_WEIGHT, 1.0, 1.0])
        _, labels, _ = cv2.kmeans(
            weighted, clusters, None, criteria, 3, cv2.KMEANS_PP_CENTERS
        )
        labels = labels.ravel()
        groups = [np.flatnonzero(labels == i) for i in range(clusters)]
        groups = _merge_close(groups, sample_lab)
        for members in groups:
            if len(members) / total < min_share:
                continue
            colors.append(_palette_entry(sample_bgr[members], total))
    colors.sort(key=lambda c: c["share"], reverse=True)
    return {"colors": colors, "paper_share": round(float(paper.sum()) / total, 4)}


def _merge_close(groups, lab, threshold=10.0):
    groups = [g for g in groups if len(g)]
    merged = True
    while merged and len(groups) > 1:
        merged = False
        means = [lab[g].mean(axis=0) for g in groups]
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                delta = means[i] - means[j]
                delta[0] *= LIGHTNESS_WEIGHT
                if float(np.sqrt(np.sum(delta * delta))) < threshold:
                    groups[i] = np.concatenate([groups[i], groups[j]])
                    del groups[j]
                    merged = True
                    break
            if merged:
                break
    return groups


def _palette_entry(bgr_pixels, total):
    # The median is robust to anti-aliased edges blending into the paper
    bgr = np.median(bgr_pixels, axis=0)
    lab = _true_lab([bgr])[0]
    return {
        "hex": to_hex(bgr),
        "share": round(len(bgr_pixels) / total, 4),
        "label_guess": label_guess(bgr),
        "lab": [round(float(v), 1) for v in lab],
    }


def is_colourful(entry, min_chroma=10.0):
    _, a, b = entry["lab"]
    return float(np.hypot(a, b)) >= min_chroma


# --------------------------------------------------------------------------- suggest


def _dropped_values(colors_bgr, spec):
    strip = np.uint8([[list(c) for c in colors_bgr]])
    return [int(v) for v in apply_dropout(strip, spec)[0]]


def suggest_dropout(target, keep=None):
    """
    Best dropout settings to make `target` (hex) disappear while `keep` colours
    (hex list; default black, pencil and blue pens) stay dark.

    Returns {"settings": colorDropout dict, "target_after": 0..255,
    "keep_after": {hex: 0..255}, "contrast": int, "good": bool,
    "candidates": [...] (every mode, best first)}.
    """
    target_bgr = parse_hex(target)
    keep = list(keep or DEFAULT_KEEP_COLORS)
    keep_bgr = [parse_hex(c) for c in keep]
    nearest = min(lab_distance(target_bgr, c) for c in keep_bgr) if keep_bgr else 120
    # Full removal within tolerance, falloff to 1.5x: stay clear of the keep colours
    tolerance = float(np.clip(nearest / (FALLOFF + 0.1), 10.0, 80.0))
    candidates = []
    # Channel modes first: they are cheapest and robust to ink-shade variation, so
    # they win ties against the colour-distance mode
    for rank, mode in enumerate(["red", "green", "blue", "max", "color"]):
        settings = {"mode": mode, "strength": 1.0}
        if mode == "color":
            settings.update(
                {"color": to_hex(target_bgr), "tolerance": round(tolerance)}
            )
        spec = normalize_dropout(settings)
        values = _dropped_values([target_bgr] + keep_bgr, spec)
        target_after, keep_after = values[0], values[1:]
        darkest_keep = max(keep_after) if keep_after else 0
        contrast = target_after - darkest_keep
        candidates.append(
            {
                "settings": settings,
                "target_after": target_after,
                "keep_after": dict(zip(keep, keep_after)),
                "contrast": contrast,
                # Small margin keeps the cheaper channel modes ahead on near ties
                "_score": contrast - rank * 3 - (12 if mode == "color" else 0),
            }
        )
    candidates.sort(key=lambda c: c["_score"], reverse=True)
    for candidate in candidates:
        del candidate["_score"]
    best = dict(candidates[0])
    best["good"] = bool(best["target_after"] >= 200 and best["contrast"] >= 90)
    best["target"] = to_hex(target_bgr)
    best["candidates"] = candidates
    return best


def analyse_sheet(image, k=6, keep=None):
    """Palette plus a dropout suggestion for each colourful (print) colour."""
    palette = extract_palette(image, k=k)
    for entry in palette["colors"]:
        if is_colourful(entry):
            suggestion = suggest_dropout(entry["hex"], keep)
            entry["suggestion"] = {
                "settings": suggestion["settings"],
                "good": suggestion["good"],
                "contrast": suggestion["contrast"],
            }
    return palette
