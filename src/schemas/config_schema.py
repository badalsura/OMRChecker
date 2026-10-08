CONFIG_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/Udayraj123/OMRChecker/tree/master/src/schemas/config-schema.json",
    "title": "Config Schema",
    "description": "OMRChecker config schema for custom tuning",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "dimensions": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "display_height": {"type": "integer"},
                "display_width": {"type": "integer"},
                "processing_height": {"type": "integer"},
                "processing_width": {"type": "integer"},
            },
        },
        "threshold_params": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "GAMMA_LOW": {"type": "number", "minimum": 0, "maximum": 1},
                "MIN_GAP": {"type": "integer", "minimum": 10, "maximum": 100},
                "MIN_JUMP": {"type": "integer", "minimum": 10, "maximum": 100},
                "CONFIDENT_SURPLUS": {"type": "integer", "minimum": 0, "maximum": 20},
                "JUMP_DELTA": {"type": "integer", "minimum": 10, "maximum": 100},
                "PAGE_TYPE_FOR_THRESHOLD": {
                    "enum": ["white", "black"],
                    "type": "string",
                },
                # "fixed": skip adaptive thresholds and use the values below
                "mode": {"enum": ["adaptive", "fixed"], "type": "string"},
                "fixed_threshold": {"type": "number", "minimum": 0, "maximum": 255},
                "fixed_min_fill_ratio": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "flatten_background": {"type": "boolean"},
            },
        },
        "alignment_params": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "auto_align": {"type": "boolean"},
                "match_col": {"type": "integer", "minimum": 0, "maximum": 10},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 100},
                "stride": {"type": "integer", "minimum": 1, "maximum": 10},
                "thickness": {"type": "integer", "minimum": 1, "maximum": 10},
                "block_snap_radius": {"type": "integer", "minimum": -1, "maximum": 50},
                "rectify_on_border": {"type": "boolean"},
                "rectify_search_px": {"type": "integer", "minimum": 2, "maximum": 100},
                "rectify_print_image": {
                    "type": "string",
                    "enum": ["auto", "grey", "darkest"],
                },
                "block_perspective": {"type": "boolean"},
                "page_outline": {"type": "boolean"},
                "verify_bubble_fit": {"type": "boolean"},
            },
        },
        "review_params": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "confidence_margin": {"type": "number", "exclusiveMinimum": 0},
                "min_grid_fit": {"type": "number", "minimum": -1, "maximum": 1},
                "min_sharpness": {"type": "number", "minimum": 0},
                "min_contrast": {"type": "number", "minimum": 0, "maximum": 255},
                "min_bubble_px": {"type": "number", "minimum": 0},
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "min_marked_fill_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "max_unmarked_fill_ratio": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                # Sheet-level too_few_marks when fewer bubbles are marked; 0 = off
                "min_marked_bubbles": {"type": "integer", "minimum": 0},
                "review_flags": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "multi_marked",
                            "empty",
                            "ambiguous_threshold",
                            "low_confidence",
                            "weak_mark",
                            "possible_missed_mark",
                            "model_disagrees",
                            "rectify_failed",
                            "border_slide",
                        ],
                    },
                },
            },
        },
        "ml_params": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "bubble_model_path": {"type": ["string", "null"]},
                "bubble_model_role": {"enum": ["second_opinion", "decide"]},
                "icr_model_path": {"type": ["string", "null"]},
            },
        },
        "pdf_params": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pdf_dpi": {
                    "anyOf": [
                        {"type": "integer", "minimum": 72, "maximum": 600},
                        {"type": "string", "enum": ["auto"]},
                    ],
                },
                "pdf_page": {
                    "anyOf": [
                        {"type": "integer", "minimum": 1},
                        {"type": "null"},
                        {"type": "string", "pattern": r"^\d+(?:-\d*)?$"},
                        {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "integer", "minimum": 1},
                                    {"type": "string", "pattern": r"^\d+(?:-\d*)?$"},
                                ]
                            },
                            "minItems": 1,
                        },
                    ],
                },
            },
        },
        "barcode_params": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "engines": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["zxing", "builtin", "opencv", "pyzbar"],
                    },
                    "minItems": 1,
                    "uniqueItems": True,
                },
                "pyzbar": {"type": "boolean"},
                "review_fallback_decodes": {"type": "boolean"},
            },
        },
        "ocr_params": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                # Engine for OCR zones without their own "engine" option
                "default_engine": {"type": "string", "enum": ["tesseract", "paddle"]},
                # Second engine for empty / low-confidence / invalid reads
                "fallback_engine": {
                    "type": "string",
                    "enum": ["none", "tesseract", "paddle"],
                },
                # PaddleOCR (PP-OCRv5 on onnxruntime) model sizes and language
                "paddle_det_model": {"type": "string", "enum": ["mobile", "server"]},
                "paddle_rec_model": {"type": "string", "enum": ["mobile", "server"]},
                "paddle_lang": {"type": "string", "enum": ["en", "ch", "devanagari"]},
                "paddle_model_dir": {"type": ["string", "null"]},
                # Tesseract traineddata set when bundled: accurate "best" or "fast"
                "tessdata": {"type": "string", "enum": ["best", "fast"]},
                # Tesseract languages for zones without "lang", e.g. ["eng", "hin"]
                "langs": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[A-Za-z_]+$"},
                    "minItems": 1,
                },
                "multi_psm": {"type": "boolean"},
                "cleanup_pass": {"type": "boolean"},
                "disagree_to_review": {"type": "boolean"},
                "user_patterns": {"type": "boolean"},
                "min_char_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                # PaddleOCR as a second reader of boxed handwriting (ICR zones)
                "icr_second_reader": {
                    "type": "string",
                    "enum": ["auto", "paddle", "none"],
                },
            },
        },
        "outputs": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "show_image_level": {"type": "integer", "minimum": 0, "maximum": 6},
                "save_image_level": {"type": "integer", "minimum": 0, "maximum": 6},
                "save_detections": {"type": "boolean"},
                # This option moves multimarked files into a separate folder for manual checking, skipping evaluation
                "filter_out_multimarked_files": {"type": "boolean"},
            },
        },
    },
}
