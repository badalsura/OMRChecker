from dotmap import DotMap

CONFIG_DEFAULTS = DotMap(
    {
        "dimensions": {
            "display_height": 2480,
            "display_width": 1640,
            "processing_height": 820,
            "processing_width": 666,
        },
        "threshold_params": {
            "GAMMA_LOW": 0.7,
            "MIN_GAP": 30,
            "MIN_JUMP": 25,
            "CONFIDENT_SURPLUS": 5,
            "JUMP_DELTA": 30,
            "PAGE_TYPE_FOR_THRESHOLD": "white",
            # "adaptive" (page and strip thresholds per sheet) or "fixed": a bubble
            # is marked when at least fixed_min_fill_ratio of its interior is
            # darker than fixed_threshold
            "mode": "adaptive",
            "fixed_threshold": 120,
            "fixed_min_fill_ratio": 0.12,
        },
        "alignment_params": {
            # Note: 'auto_align' enables automatic template alignment, use if the scans show slight misalignments.
            "auto_align": False,
            "match_col": 5,
            "max_steps": 20,
            "stride": 1,
            "thickness": 3,
            # Search radius (px) for snapping each field block onto its printed
            # bubbles in both directions after registration; 0 disables it
            "block_snap_radius": 0,
        },
        "review_params": {
            # Intensity distance from the threshold that counts as fully confident
            "confidence_margin": 20,
            # Fields whose weakest bubble is below this confidence get flagged
            "min_confidence": 0.35,
            # A marked bubble with less of its interior filled than this is suspicious
            "min_marked_fill_ratio": 0.25,
            # An unmarked bubble with more of its interior filled than this is suspicious
            "max_unmarked_fill_ratio": 0.6,
            # Flag the sheet (too_few_marks) when fewer bubbles are marked; 0 = off.
            # Catches pens the colour dropout removed along with the print
            "min_marked_bubbles": 0,
            # Flags that send a field to the manual review queue
            "review_flags": [
                "multi_marked",
                "ambiguous_threshold",
                "low_confidence",
                "weak_mark",
                "possible_missed_mark",
                "model_disagrees",
            ],
        },
        "ml_params": {
            # ONNX crop classifiers (see src/ml); relative paths resolve from the template folder
            "bubble_model_path": None,
            "icr_model_path": None,
        },
        "pdf_params": {
            "pdf_dpi": "auto",
            "pdf_page": 1,
        },
        "barcode_params": {
            # Barcode/QR decoders, tried in order until one reads (src/readers/barcode.py)
            "engines": ["zxing", "builtin", "opencv", "pyzbar"],
            # pyzbar (ZBar) is optional: used only when installed and switched on
            "pyzbar": False,
            # Send zones read by an engine other than zxing to review
            "review_fallback_decodes": False,
        },
        "outputs": {
            "show_image_level": 0,
            "save_image_level": 0,
            "save_detections": True,
            "filter_out_multimarked_files": False,
        },
    },
    _dynamic=False,
)
