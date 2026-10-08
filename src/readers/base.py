import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from src.logger import logger


@dataclass
class ZoneReadResult:
    name: str
    type: str
    value: str
    confidence: float
    flags: List[str] = field(default_factory=list)
    needs_review: bool = False
    box: List[int] = field(default_factory=list)
    format: Optional[str] = None
    details: dict = field(default_factory=dict)
    # Which decoder produced the value (barcode/QR zones), e.g. "zxing", "builtin"
    engine: Optional[str] = None

    def to_dict(self):
        return asdict(self)


# Flags that always send a zone to manual review
ZONE_REVIEW_FLAGS = {
    "not_found",
    "multiple_symbols",
    "low_confidence",
    "pattern_mismatch",
    "engine_unavailable",
    "no_icr_model",
    "read_error",
    # OCR: two engines read different text; one character below minCharConfidence
    "engine_disagree",
    "low_char_confidence",
}


def finalize(
    result: ZoneReadResult, zone, min_confidence_default=0.6, extra_review_flags=()
):
    options = zone.options
    pattern = options.get("pattern")
    if pattern and result.value and not re.fullmatch(pattern, result.value):
        result.flags.append("pattern_mismatch")
    min_confidence = options.get("minConfidence", min_confidence_default)
    if result.value and result.confidence < min_confidence:
        result.flags.append("low_confidence")
    result.flags = sorted(set(result.flags))
    result.needs_review = any(
        flag in ZONE_REVIEW_FLAGS or flag in extra_review_flags for flag in result.flags
    )
    if not result.value:
        result.value = zone.empty_val
    return result


def read_zone(zone, image, engines=None):
    from src.readers import barcode, icr, image_zone, ocr

    engines = engines or {}
    box = [*zone.origin, *zone.dimensions]
    try:
        if zone.type in ("barcode", "qrcode"):
            result = barcode.read_barcode_zone(
                zone, image, engines.get("barcode_params")
            )
        elif zone.type == "ocr":
            result = ocr.read_ocr_zone(zone, image, engines.get("ocr_params"))
        elif zone.type == "icr":
            result = icr.read_icr_zone(
                zone, image, engines.get("icr"), engines.get("ocr_params")
            )
        elif zone.type == "image":
            result = image_zone.read_image_zone(zone, image)
        else:
            raise ValueError(f"Unknown zone type: {zone.type}")
    except Exception as error:  # a broken zone must not lose the rest of the sheet
        logger.error(f"Failed to read zone '{zone.name}': {error}")
        result = ZoneReadResult(
            zone.name, zone.type, "", 0.0, ["read_error"], details={"error": str(error)}
        )
    result.box = box
    barcode_params = engines.get("barcode_params") or {}
    extra = (
        ("decoded_by_fallback",)
        if barcode_params.get("review_fallback_decodes")
        else ()
    )
    return finalize(result, zone, extra_review_flags=extra)


def skipped_zone(zone):
    """Placeholder for a lazy zone that was not needed (see src/rules/checks.py)."""
    return ZoneReadResult(
        zone.name,
        zone.type,
        zone.empty_val,
        0.0,
        ["not_read"],
        box=[*zone.origin, *zone.dimensions],
    )


def read_zones(zones, image, engines=None):
    """Read every zone; lazy zones (fallbacks) are read later only if needed."""
    return {
        zone.name: (
            skipped_zone(zone)
            if getattr(zone, "lazy", False)
            else read_zone(zone, image, engines)
        )
        for zone in zones
    }
