"""
Image zones: keep a crop of the sheet (student photo, signature) with each result.

Zone options (template.json):
- saveFilename: file name pattern, default "{file}_{zone}.png". {file} is the
  sheet's file name without extension (PDF pages: "<name>_p<n>"), {zone} the
  zone name, {page} the PDF page number ("" otherwise). The crop is written by whoever stores the
  result (API: the scan folder; CLI/batch: the output folder) and the zone value
  is that file name.
- embedBase64: also put the crop, as a PNG data URI, into the zone details
  (details.image_base64), so database records and API responses carry it.
  Off by default; only small crops are embedded (see EMBED_MAX_BYTES).
- maxSide: downscale the crop so its longer side is at most this many pixels.
"""

import base64
import re
from pathlib import Path

import cv2

from src.logger import logger
from src.readers.base import ZoneReadResult

DEFAULT_FILENAME = "{file}_{zone}.png"
# A crop is embedded only when its PNG is at most this large
EMBED_MAX_BYTES = 200 * 1024
# Longer side of an embedded crop when the zone sets no maxSide
EMBED_DEFAULT_MAX_SIDE = 400
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def limit_side(image, max_side):
    if not max_side:
        return image
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return image
    scale = float(max_side) / longest
    size = (max(int(round(w * scale)), 1), max(int(round(h * scale)), 1))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def read_image_zone(zone, image):
    crop = limit_side(zone.crop(image), zone.options.get("maxSide"))
    h, w = crop.shape[:2]
    result = ZoneReadResult(
        zone.name, zone.type, "", 1.0, [], details={"width": int(w), "height": int(h)}
    )
    # Kept out of to_dict(); attach_zone_images() names it and the caller saves it
    result.crop = crop
    return result


def zone_filename(pattern, file_id, zone_name):
    """Safe relative file name for a zone crop."""
    stem = Path(Path(str(file_id)).name).stem or "sheet"
    # PDF pages are named "<file>_p<n>.png" (src/utils/image.py)
    match = re.search(r"_p(\d+)$", stem)
    page = match.group(1) if match else ""
    try:
        filename = (pattern or DEFAULT_FILENAME).format(
            file=stem, zone=zone_name, page=page
        )
    except (KeyError, IndexError, ValueError):
        filename = DEFAULT_FILENAME.format(file=stem, zone=zone_name)
    filename = _UNSAFE.sub("_", Path(filename).name).strip("._") or f"{stem}_{zone_name}"
    if Path(filename).suffix.lower() not in (".png", ".jpg", ".jpeg"):
        filename += ".png"
    return filename


def encode_data_uri(crop, max_side=None):
    small = limit_side(crop, max_side or EMBED_DEFAULT_MAX_SIDE)
    ok, buffer = cv2.imencode(".png", small)
    if not ok:
        return None
    data = buffer.tobytes()
    if len(data) > EMBED_MAX_BYTES:
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def attach_zone_images(zone_results, zones, file_id):
    """
    Name every image zone's crop after the sheet, embed base64 where asked.
    zone_results: {name: ZoneReadResult}; zones: template zones.
    Returns {file name: crop} for the caller to write.
    """
    images = {}
    by_name = {zone.name: zone for zone in zones}
    for name, result in zone_results.items():
        zone = by_name.get(name)
        crop = getattr(result, "crop", None)
        if zone is None or zone.type != "image" or crop is None:
            continue
        filename = zone_filename(zone.options.get("saveFilename"), file_id, name)
        result.value = filename
        result.details["file"] = filename
        images[filename] = crop
        if zone.options.get("embedBase64"):
            uri = encode_data_uri(crop, zone.options.get("maxSide"))
            if uri:
                result.details["image_base64"] = uri
            else:
                result.details["image_base64_skipped"] = "too_large"
    return images


def save_zone_images(images, directory):
    """Write {file name: crop} into directory; returns the paths written."""
    if not images:
        return []
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, crop in images.items():
        path = directory / Path(filename).name
        try:
            if cv2.imwrite(str(path), crop):
                written.append(path)
        except Exception as error:  # a crop must never fail the sheet
            logger.warning(f"Could not save zone image {path}: {error}")
    return written
