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
                "block_snap_radius": {"type": "integer", "minimum": 0, "maximum": 50},
                "rectify_on_border": {"type": "boolean"},
                "rectify_search_px": {"type": "integer", "minimum": 2, "maximum": 100},
            },
        },
        "review_params": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "confidence_margin": {"type": "number", "exclusiveMinimum": 0},
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
