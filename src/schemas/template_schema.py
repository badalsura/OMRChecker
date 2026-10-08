from src.constants.common import FIELD_TYPES
from src.schemas.constants import ARRAY_OF_STRINGS, FIELD_STRING_TYPE

positive_number = {"type": "number", "minimum": 0}
positive_integer = {"type": "integer", "minimum": 0}
two_positive_integers = {
    "type": "array",
    "prefixItems": [
        positive_integer,
        positive_integer,
    ],
    "maxItems": 2,
    "minItems": 2,
}
two_positive_numbers = {
    "type": "array",
    "prefixItems": [
        positive_number,
        positive_number,
    ],
    "maxItems": 2,
    "minItems": 2,
}
zero_to_one_number = {
    "type": "number",
    "minimum": 0,
    "maximum": 1,
}

TIMING_MARK_TRACK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["marks"],
    "properties": {
        # Expected centres of the marks along this track, in template (page) pixels
        "marks": {
            "type": "array",
            "items": two_positive_numbers,
            "minItems": 2,
        },
    },
}

INDEX_POINT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["center", "size"],
    "properties": {
        "name": {"type": "string"},
        # Centre and [width, height] in template pixels
        "center": two_positive_numbers,
        "size": two_positive_numbers,
        "shape": {"type": "string", "enum": ["square", "circle", "any"]},
        # A missing required point sends the sheet to review
        "required": {"type": "boolean"},
    },
}

# Template-level alignment settings (override config.json alignment_params)
ALIGNMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "rectify_on_border": {"type": "boolean"},
        "rectify_search_px": {"type": "integer", "minimum": 2, "maximum": 100},
        "rectify_print_image": {"type": "string", "enum": ["auto", "grey", "darkest"]},
        "block_perspective": {"type": "boolean"},
        "page_outline": {"type": "boolean"},
        "verify_bubble_fit": {"type": "boolean"},
    },
}

TIMING_MARK_OPTIONS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tracks", "markDimensions"],
    "properties": {
        # Tracks may be {} when indexPoints alone register the sheet
        "tracks": {
            "type": "object",
            "patternProperties": {"^.*$": TIMING_MARK_TRACK_SCHEMA},
        },
        # [width, height] of a single timing mark in template pixels
        "markDimensions": two_positive_numbers,
        # Allowed relative deviation in mark size before a blob is rejected
        "sizeTolerance": zero_to_one_number,
        # How far (template px) a detected mark may sit from its expected position
        "searchRadius": positive_number,
        "minMatchedMarks": positive_integer,
        # Mean reprojection error (template px) above which the sheet is rejected
        "maxResidual": positive_number,
        # Apply a thin-plate-spline refinement on top of the homography;
        # "auto": only when the reference points spread across the page
        "nonRigid": {"enum": [True, False, "auto", "tracks"]},
        # Try 90/180/270 degree rotations when the sheet is fed in wrongly
        "detectOrientation": {"type": "boolean"},
        "earlyStop": {"type": "boolean"},
        "indexSeed": {"type": "boolean"},
        # Index points (corner squares, dots, L-corners): fitted together with
        # the timing marks; each keeps its own size and shape
        "indexPoints": {"type": "array", "items": INDEX_POINT_SCHEMA},
        # One homography from index points + timing marks + printed block corners
        "jointFit": {"type": "boolean"},
        # Report (and trim) scan margins outside the template page
        "trimMargins": {"type": "boolean"},
        # Mean residual (template px) of any page region above which the
        # sheet goes to review
        "maxRegionResidual": positive_number,
    },
}

BARCODE_ENGINES = ["zxing", "builtin", "opencv", "pyzbar"]

# Length or range bounds: a number, or [min, max] where either may be null
BOUNDS_SCHEMA = {
    "anyOf": [
        {"type": "number"},
        {
            "type": "array",
            "items": {"type": ["number", "null"]},
            "minItems": 1,
            "maxItems": 2,
        },
    ]
}

NORMALIZE_SCHEMA = {
    "anyOf": [
        {"type": "string", "enum": ["none", "strip", "digits", "upper", "alnum"]},
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["regex"],
            "properties": {
                "regex": {"type": "string"},
                "group": {"type": ["integer", "string"]},
            },
        },
    ]
}

# Colour dropout: how a colour scan becomes the grey image that is read
COLOR_DROPOUT_SCHEMA = {
    "anyOf": [
        # Shorthand for {"mode": ...}
        {"type": "string", "enum": ["grey", "gray", "red", "green", "blue", "max"]},
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["grey", "gray", "red", "green", "blue", "max", "color"],
                },
                # Colour to remove in "color" mode, e.g. "#E8618C"
                "color": {"type": "string", "pattern": "^#?([0-9a-fA-F]{3}){1,2}$"},
                # Lab colour distance treated as a full match (soft falloff to 1.5x)
                "tolerance": {"type": "number", "minimum": 0, "maximum": 200},
                # 0 = plain grey, 1 = full dropout
                "strength": zero_to_one_number,
            },
            "if": {"properties": {"mode": {"const": "color"}}},
            "then": {"required": ["color"]},
        },
    ]
}

# "validate": {"<field, custom label, zone or check output>": {...}}
VALIDATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "length": BOUNDS_SCHEMA,
        "required": {"type": "boolean"},
        "allowGaps": {"type": "boolean"},
        "allowEmptyEnds": {"type": "boolean"},
        "leadingZeros": {"type": "string", "enum": ["keep", "forbid"]},
        "pattern": {"type": "string"},
        "allowed": {"type": "array", "items": {"type": ["string", "number"]}},
        "range": BOUNDS_SCHEMA,
        "onFail": {"type": "string", "enum": ["review", "blank", "both", "flag"]},
    },
}

# "checks": [{...}] cross-field rules (src/rules/checks.py)
CHECK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "sources"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "sources": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "priority": {"type": "array", "items": {"type": "string"}},
        "normalize": NORMALIZE_SCHEMA,
        "onMissing": {"type": "string", "enum": ["fallback", "review"]},
        "onConflict": {"type": "string", "enum": ["prefer", "review", "error"]},
        "reviewOnConflict": {"type": "boolean"},
        "reviewOnFallback": {"type": "boolean"},
        "reviewOnAllMissing": {"type": "boolean"},
        "skipInvalid": {"type": "boolean"},
        "skipFlagged": {"type": "boolean"},
        "absorbSourceReview": {"type": "boolean"},
        "output": {"type": "string", "minLength": 1},
    },
}

ZONE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "origin", "dimensions"],
    "properties": {
        "type": {
            "type": "string",
            "enum": ["barcode", "qrcode", "ocr", "icr", "image"],
        },
        "origin": two_positive_integers,
        "dimensions": two_positive_integers,
        "options": {
            "type": "object",
            "properties": {
                # Barcode / QR: restrict accepted symbologies, e.g. ["Code128", "QRCode"]
                "formats": ARRAY_OF_STRINGS,
                # OCR / ICR: restrict characters, e.g. "0123456789"
                "whitelist": {"type": "string"},
                "lang": {"type": "string"},
                "psm": {"type": "integer", "minimum": 0, "maximum": 13},
                # ICR: number of equal-width boxed characters in the zone
                "characterBoxes": {"type": "integer", "minimum": 1},
                # Regex the final value must match, otherwise it is flagged for review
                "pattern": {"type": "string"},
                "minConfidence": zero_to_one_number,
                "emptyValue": {"type": "string"},
                # Barcode / QR: decoder order and optional engines
                "engines": {
                    "type": "array",
                    "items": {"type": "string", "enum": BARCODE_ENGINES},
                    "minItems": 1,
                },
                "pyzbar": {"type": "boolean"},
                # Built-in decoder: Code 39 mod 43 check digit / full ASCII,
                # ITF GS1 check digit and minimum length
                "code39Checksum": {"type": "boolean"},
                "code39Extended": {"enum": ["auto", True, False]},
                "itfChecksum": {"type": "boolean"},
                "itfMinLength": {"type": "integer", "minimum": 2},
                # Barcode: zone (e.g. OCR of the printed digits) read only when
                # the barcode gives nothing; creates a check named after this zone
                "fallbackZone": {"type": "string"},
                "reviewOnFallback": {"type": "boolean"},
                "fallbackNormalize": NORMALIZE_SCHEMA,
                # Read only when a check needs this zone as a fallback
                "lazy": {"type": "boolean"},
                # Read this zone from a differently processed image (e.g. "grey")
                "colorDropout": COLOR_DROPOUT_SCHEMA,
                # OCR / ICR: turn the crop before reading; "rot90cw" for vertical
                # text reading bottom to top, "rot90ccw" top to bottom, "auto"
                # tries all four (src/readers/text_reader.py)
                "direction": {
                    "type": "string",
                    "enum": ["horizontal", "rot90cw", "rot90ccw", "rot180", "auto"],
                },
                # OCR: engine for this zone and the one tried when its read is
                # empty, low-confidence or invalid ("default": config ocr_params)
                "engine": {
                    "type": "string",
                    "enum": ["default", "tesseract", "paddle"],
                },
                "fallbackEngine": {
                    "type": "string",
                    "enum": ["default", "none", "tesseract", "paddle"],
                },
                # OCR: retry other Tesseract layout modes on an empty / invalid read
                "psmRetry": {"type": "boolean"},
                # OCR: Tesseract user patterns, e.g. ["\\d\\d\\d\\d\\d\\d\\d"]
                "userPatterns": ARRAY_OF_STRINGS,
                # OCR: send to review when any character is below this confidence
                "minCharConfidence": zero_to_one_number,
                # Image zones: crop file name pattern ({file}, {zone}, {page}),
                # base64 copy in results/API (small crops only), longer side px
                "saveFilename": {"type": "string", "minLength": 1},
                "embedBase64": {"type": "boolean"},
                "maxSide": {"type": "integer", "minimum": 16},
            },
        },
    },
}

TEMPLATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/Udayraj123/OMRChecker/tree/master/src/schemas/template-schema.json",
    "title": "Template Validation Schema",
    "description": "OMRChecker input template schema",
    "type": "object",
    "required": [
        "bubbleDimensions",
        "pageDimensions",
        "preProcessors",
        "fieldBlocks",
    ],
    "additionalProperties": False,
    "properties": {
        "bubbleDimensions": {
            **two_positive_integers,
            "description": "The dimensions of the overlay bubble area: [width, height]",
        },
        "customLabels": {
            "description": "The customLabels contain fields that need to be joined together before generating the results sheet",
            "type": "object",
            "patternProperties": {
                "^.*$": {"type": "array", "items": FIELD_STRING_TYPE}
            },
        },
        "groupOptions": {
            "description": "Per customLabels group: what an empty, multi-marked or flagged column becomes (one character per column). A group without an entry keeps the plain join",
            "type": "object",
            "patternProperties": {
                "^.*$": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        # Placeholder for an unmarked column; null skips the column
                        "empty": {"type": ["string", "null"]},
                        "multi": {"type": "string"},
                        "issue": {"type": "string"},
                    },
                }
            },
        },
        "primaryKey": {
            "description": "Output columns that identify a sheet; sheets sharing them are duplicates",
            "type": "array",
            "items": {"type": "string"},
        },
        "outputColumns": {
            "type": "array",
            "items": FIELD_STRING_TYPE,
            "description": "The ordered list of columns to be contained in the output csv(default order: alphabetical)",
        },
        "pageDimensions": {
            **two_positive_integers,
            "description": "The dimensions(width, height) to which the page will be resized to before applying template",
        },
        "preProcessors": {
            "description": "Custom configuration values to use in the template's directory",
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": [
                            "CropOnMarkers",
                            "CropPage",
                            "EccAlignment",
                            "FeatureBasedAlignment",
                            "TimingMarkAlignment",
                            "GaussianBlur",
                            "Levels",
                            "MedianBlur",
                        ],
                    },
                },
                "required": ["name", "options"],
                "allOf": [
                    {
                        "if": {"properties": {"name": {"const": "CropOnMarkers"}}},
                        "then": {
                            "properties": {
                                "options": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "apply_erode_subtract": {"type": "boolean"},
                                        "marker_rescale_range": two_positive_numbers,
                                        "marker_rescale_steps": {"type": "number"},
                                        "max_matching_variation": {"type": "number"},
                                        "min_matching_threshold": {"type": "number"},
                                        "relativePath": {"type": "string"},
                                        "sheetToMarkerWidthRatio": {"type": "number"},
                                    },
                                    "required": ["relativePath"],
                                }
                            }
                        },
                    },
                    {
                        "if": {
                            "properties": {"name": {"const": "FeatureBasedAlignment"}}
                        },
                        "then": {
                            "properties": {
                                "options": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "2d": {"type": "boolean"},
                                        "goodMatchPercent": {"type": "number"},
                                        "maxFeatures": {"type": "integer"},
                                        "maxScaleChange": {"type": "number"},
                                        "minInliers": {"type": "integer"},
                                        "reference": {"type": "string"},
                                    },
                                    "required": ["reference"],
                                }
                            }
                        },
                    },
                    {
                        "if": {"properties": {"name": {"const": "Levels"}}},
                        "then": {
                            "properties": {
                                "options": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "gamma": zero_to_one_number,
                                        "high": zero_to_one_number,
                                        "low": zero_to_one_number,
                                    },
                                }
                            }
                        },
                    },
                    {
                        "if": {"properties": {"name": {"const": "MedianBlur"}}},
                        "then": {
                            "properties": {
                                "options": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {"kSize": {"type": "integer"}},
                                }
                            }
                        },
                    },
                    {
                        "if": {"properties": {"name": {"const": "GaussianBlur"}}},
                        "then": {
                            "properties": {
                                "options": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "kSize": two_positive_integers,
                                        "sigmaX": {"type": "number"},
                                    },
                                }
                            }
                        },
                    },
                    {
                        "if": {
                            "properties": {"name": {"const": "TimingMarkAlignment"}}
                        },
                        "then": {
                            "properties": {
                                "options": TIMING_MARK_OPTIONS_SCHEMA,
                            }
                        },
                    },
                    {
                        "if": {"properties": {"name": {"const": "EccAlignment"}}},
                        "then": {
                            "properties": {
                                "options": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "reference": {"type": "string"},
                                        "motion": {
                                            "type": "string",
                                            "enum": [
                                                "translation",
                                                "euclidean",
                                                "affine",
                                                "homography",
                                            ],
                                        },
                                        "iterations": {"type": "integer", "minimum": 1},
                                        "epsilon": {"type": "number"},
                                        "scale": zero_to_one_number,
                                        "minCorrelation": zero_to_one_number,
                                    },
                                    "required": ["reference"],
                                }
                            }
                        },
                    },
                    {
                        "if": {"properties": {"name": {"const": "CropPage"}}},
                        "then": {
                            "properties": {
                                "options": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "morphKernel": two_positive_integers
                                    },
                                }
                            }
                        },
                    },
                ],
            },
        },
        "fieldBlocks": {
            "description": "The fieldBlocks denote small groups of adjacent fields",
            "type": "object",
            "patternProperties": {
                "^.*$": {
                    "type": "object",
                    "required": [
                        "origin",
                        "bubblesGap",
                        "labelsGap",
                        "fieldLabels",
                    ],
                    "oneOf": [
                        {"required": ["fieldType"]},
                        {"required": ["bubbleValues", "direction"]},
                    ],
                    "properties": {
                        "bubbleDimensions": two_positive_numbers,
                        "bubblesGap": positive_number,
                        "bubbleValues": ARRAY_OF_STRINGS,
                        "direction": {
                            "type": "string",
                            "enum": ["horizontal", "vertical"],
                        },
                        "emptyValue": {"type": "string"},
                        "fieldLabels": {"type": "array", "items": FIELD_STRING_TYPE},
                        "labelsGap": positive_number,
                        "origin": two_positive_integers,
                        "fieldType": {
                            "type": "string",
                            "enum": list(FIELD_TYPES.keys()),
                        },
                        # Fit the block onto its printed rectangular border after
                        # page alignment (overrides alignment_params.rectify_on_border)
                        "rectifyOnBorder": {"type": "boolean"},
                        # Gap between the bubbles' bounding box and the printed
                        # border, [x, y] or one number; default: estimated per sheet
                        "borderPadding": {
                            "anyOf": [positive_number, two_positive_numbers]
                        },
                        # Two-level search: gap to a large outer frame found
                        # first; the inner border is then searched relative to it
                        "outerBorderPadding": {
                            "anyOf": [positive_number, two_positive_numbers]
                        },
                        # Fit the block's perspective to its printed bubble
                        # outlines when it has no printed box (overrides
                        # alignment block_perspective)
                        "blockPerspective": {"type": "boolean"},
                    },
                }
            },
        },
        "emptyValue": {
            "description": "The value to be used in case of empty bubble detected at global level.",
            "type": "string",
        },
        "alignment": {
            **ALIGNMENT_SCHEMA,
            "description": "Alignment settings for this template (override config.json alignment_params)",
        },
        "colorDropout": {
            **COLOR_DROPOUT_SCHEMA,
            "description": "How a colour scan is turned into the grey image that is read (default: plain grey)",
        },
        "zones": {
            "description": "Non-bubble regions to read: barcodes, QR codes, printed text (OCR) and handwriting (ICR)",
            "type": "object",
            "patternProperties": {"^.*$": ZONE_SCHEMA},
        },
        "validate": {
            "description": "Shape checks per output (field, custom label, zone or check output): length, gaps, pattern, allowed values, range",
            "type": "object",
            "patternProperties": {"^.*$": VALIDATION_SCHEMA},
        },
        "checks": {
            "description": "Cross-field rules combining several reads of one value (e.g. barcode with OCR fallback)",
            "type": "array",
            "items": CHECK_SCHEMA,
        },
    },
}
