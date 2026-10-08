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
            # Divide the page by a smooth background estimate before measuring
            # bubbles, so shadows and uneven light don't shift the threshold
            "flatten_background": True,
        },
        "alignment_params": {
            # Note: 'auto_align' enables automatic template alignment, use if the scans show slight misalignments.
            "auto_align": False,
            "match_col": 5,
            "max_steps": 20,
            "stride": 1,
            "thickness": 3,
            # Search radius (px) for snapping each field block onto its printed
            # bubbles in both directions after registration; 0 disables it,
            # -1 = automatic (0.3 x the block's bubble pitch). A block is never
            # moved by 0.4 pitch or more (that would be a neighbouring bubble).
            # Off by default: it fixes curl on synthetic sheets but changed
            # answers on some of the bundled real samples
            "block_snap_radius": 0,
            # Fit each field block onto its printed rectangular border after page
            # alignment (a block's "rectifyOnBorder" overrides this)
            "rectify_on_border": False,
            # How far (px) the border may sit from where the template expects it
            "rectify_search_px": 20,
            # Image the borders are searched on when a colour dropout removes
            # the print: "auto"/"darkest" (darkest channel) or "grey"
            "rectify_print_image": "auto",
            # Fit blocks without a printed box onto their bubble outlines
            "block_perspective": False,
            # Find the page outline in phone photos before registration
            "page_outline": False,
            # Reject a block correction that makes the printed bubbles fit worse
            "verify_bubble_fit": True,
        },
        "review_params": {
            # Registration check: a sheet whose blocks' printed bubbles correlate
            # below this with where the template puts them is flagged
            # registration_suspect (0 disables; forms without printed bubble
            # outlines should disable it). Off by default: 0.05 separated
            # misregistered synthetic sheets but also flagged 5 good real samples
            "min_grid_fit": 0,
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
        "ocr_params": {
            # OCR engines (src/readers/text_reader.py). A build may change these
            # defaults through ocr_build.json (packaging/ocr_models.md)
            "default_engine": "tesseract",
            "fallback_engine": "none",
            "paddle_det_model": "mobile",
            "paddle_rec_model": "mobile",
            "paddle_lang": "en",
            "paddle_model_dir": None,
            # "best" traineddata when the build bundled it, else the system install
            "tessdata": "best",
            "langs": ["eng"],
            # Retry other layout modes on an empty / invalid read
            "multi_psm": True,
            # Second binarisation (denoise + adaptive threshold) at low confidence
            "cleanup_pass": True,
            # Two engines reading different text send the zone to review
            "disagree_to_review": True,
            # Pass the zone pattern to Tesseract as user patterns
            "user_patterns": True,
            # Review when any character is below this confidence; 0 = off
            "min_char_confidence": 0,
            # PaddleOCR reads boxed handwriting as well when its models are installed
            "icr_second_reader": "auto",
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

# A build may choose other OCR defaults (packaging/ocr_build.json)
from src.readers.ocr_build import apply_build_defaults  # noqa: E402

apply_build_defaults(CONFIG_DEFAULTS)
