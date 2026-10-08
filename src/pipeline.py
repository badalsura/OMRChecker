"""
Library entry point for reading OMR sheets without the CLI's folder conventions.

    engine = OMREngine("samples/sample1/template.json")
    flag = cv2.IMREAD_COLOR if engine.needs_color else cv2.IMREAD_GRAYSCALE
    result = engine.scan(cv2.imread("sheet.jpg", flag), "sheet.jpg")
    result.to_dict()

Colour: scan() takes a BGR or grey image. A BGR image goes through the
template's colorDropout (and per-zone overrides); a grey image is read as-is.
engine.needs_color says whether decoding in colour is worth it; scan_path()
already does the right thing for files and PDFs.

Regrading with other colour settings, without touching the template files:

    OMREngine(path, template_overrides={"colorDropout": "grey"})
    OMREngine(path, template_overrides={"colorDropout": {"mode": "red"}})

template_overrides replaces top-level template keys (None removes one) and is
validated like the file. engine.set_color_dropout(spec) changes the page
dropout of an existing engine.

An engine is cheap to call repeatedly but not thread-safe: preprocessors and
the reading code keep per-instance state. Use one engine per worker thread or
process (see src/batch.py).
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from dotmap import DotMap

from src.defaults import CONFIG_DEFAULTS
from src.geometry import GeometryRecorder, print_kept_image
from src.evaluation import (
    EvaluationConfig,
    evaluate_concatenated_response_detailed,
    scoring_summary,
)
from src.logger import logger
from src.quality import measure as measure_quality
from src.quality import review as quality_review
from src.ml.classifiers import load_crop_classifier
from src.readers import read_zone, read_zones
from src.readers.image_zone import attach_zone_images
from src.rules import review_items
from src.template import Template
from src.utils.image import ImageUtils
from src.utils.parsing import (
    describe_groups,
    get_concatenated_response,
    group_review_items,
    open_config_with_defaults,
)

STATUS_OK = "ok"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_ERROR = "error"
# Companion key of the print-kept copy registered alongside the page
PRINT_COMPANION = "__print_kept__"


@dataclass
class ScanResult:
    file_id: str
    status: str
    responses: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    zones: dict = field(default_factory=dict)
    review: list = field(default_factory=list)
    score: Optional[float] = None
    error: Optional[str] = None
    thresholds: dict = field(default_factory=dict)
    timings_ms: dict = field(default_factory=dict)
    # Cross-field checks and value validation (template "checks"/"validate")
    checks: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    # How the sheet was mapped onto the template (src/geometry.py); None when
    # a step could not be recorded
    geometry: Optional[dict] = None
    # Score details: max_score, per-section scores, verdict counts, band
    scoring: dict = field(default_factory=dict)
    # Groups with groupOptions: per-column states (src/utils/parsing.py)
    groups: dict = field(default_factory=dict)
    # Image quality measures (src/quality.py)
    quality: Optional[dict] = None
    # Images are kept out of to_dict(); callers decide whether to persist them
    aligned_image: Optional[np.ndarray] = None
    marked_image: Optional[np.ndarray] = None
    # Aligned copy that keeps the printed form (border search), when made
    print_image: Optional[np.ndarray] = None
    # Image zone crops {file name: image}; save with src.readers.image_zone.save_zone_images
    zone_images: dict = field(default_factory=dict)

    def to_dict(self):
        out = {
            "file_id": self.file_id,
            "status": self.status,
            "responses": self.responses,
            "fields": self.fields,
            "zones": self.zones,
            "review": self.review,
            "score": self.score,
            "error": self.error,
            "thresholds": self.thresholds,
            "timings_ms": self.timings_ms,
            "checks": self.checks,
            "validation": self.validation,
            "scoring": self.scoring,
            **({"groups": self.groups} if self.groups else {}),
        }
        if self.geometry is not None:
            out["geometry"] = self.geometry
        if self.quality is not None:
            out["quality"] = self.quality
        return out


def resolve_path(base_dir, path):
    if not path:
        return None
    path = Path(path)
    return path if path.is_absolute() else Path(base_dir, path)


class OMREngine:
    def __init__(
        self,
        template_path,
        config_path=None,
        evaluation_path=None,
        config_overrides=None,
        bubble_model_path=None,
        icr_model_path=None,
        template_overrides=None,
    ):
        template_path = Path(template_path)
        template_dir = template_path.parent
        if config_path is None and template_dir.joinpath("config.json").exists():
            config_path = template_dir.joinpath("config.json")
        if config_path:
            tuning_config = open_config_with_defaults(config_path)
        else:
            tuning_config = DotMap(CONFIG_DEFAULTS.toDict(), _dynamic=False)
        tuning_config = tuning_config.toDict()
        # A library call never opens windows or writes debug stacks
        tuning_config["outputs"]["show_image_level"] = 0
        tuning_config["outputs"]["save_image_level"] = 0
        for section, values in (config_overrides or {}).items():
            tuning_config.setdefault(section, {}).update(values)
        self.tuning_config = DotMap(tuning_config, _dynamic=False)

        self.template = Template(
            template_path, self.tuning_config, overrides=template_overrides
        )
        self.evaluation_config = None
        if (
            evaluation_path is None
            and template_dir.joinpath("evaluation.json").exists()
        ):
            evaluation_path = template_dir.joinpath("evaluation.json")
        if evaluation_path:
            self.evaluation_config = EvaluationConfig(
                template_dir, Path(evaluation_path), self.template, self.tuning_config
            )
            # Explanations are console tables meant for the CLI
            self.evaluation_config.should_explain_scoring = False
            if not self.evaluation_config.grade_enabled:
                # "grade": false keeps the answer key but switches scoring off
                self.evaluation_config = None

        ml_params = self.tuning_config.ml_params
        self.image_ops = self.template.image_instance_ops
        self.image_ops.bubble_classifier = load_crop_classifier(
            resolve_path(template_dir, bubble_model_path or ml_params.bubble_model_path)
        )
        self.image_ops.model_decides = ml_params.get("bubble_model_role") == "decide"
        self.zone_engines = {
            "icr": load_crop_classifier(
                resolve_path(template_dir, icr_model_path or ml_params.icr_model_path)
            ),
            "barcode_params": self.tuning_config.barcode_params.toDict(),
            "ocr_params": (
                self.tuning_config.ocr_params.toDict()
                if "ocr_params" in self.tuning_config
                else {}
            ),
        }

    @property
    def needs_color(self):
        """Decode sheets in colour for this template (a colour dropout is set)."""
        return self.template.needs_color

    def set_color_dropout(self, spec):
        """Change the page colorDropout (dict, mode string or None for grey)."""
        self.template.set_color_dropout(spec)

    def scan(self, image, file_id="image", keep_images=True):
        """Read one sheet image: BGR (colour dropout applies) or grayscale."""
        timings = {}
        started = time.perf_counter()
        if image is None:
            return ScanResult(file_id, STATUS_ERROR, error="Image could not be read")
        recorder = GeometryRecorder(image.shape[1], image.shape[0])
        source = image
        # Colour dropout; variants exist only for zones with their own setting
        print_source = None
        if image.ndim == 3:
            if self.needs_print_image():
                print_source = print_kept_image(
                    image,
                    self.image_ops.alignment_option(
                        self.template, "rectify_print_image", "auto"
                    ),
                )
            image, variants = self.template.prepare_image(image)
            timings["dropout"] = _elapsed_ms(started)
        else:
            variants = {}

        self.image_ops.reset_all_save_img()
        companions = dict(variants)
        if print_source is not None:
            companions[PRINT_COMPANION] = print_source
        self.image_ops.geometry_recorder = recorder
        try:
            aligned = self.image_ops.apply_preprocessors(
                file_id, image, self.template, companions or None
            )
        finally:
            self.image_ops.geometry_recorder = None
        print_aligned = companions.pop(PRINT_COMPANION, None)
        variants = companions
        timings["registration"] = _elapsed_ms(started)
        if aligned is None:
            return ScanResult(
                file_id,
                STATUS_ERROR,
                error="Sheet registration failed (page, markers or timing marks not found)",
                timings_ms=timings,
            )

        step = time.perf_counter()
        detailed = self.image_ops.read_omr_response_detailed(
            self.template, aligned, file_id, save_dir=None, print_image=print_aligned
        )
        timings["bubbles"] = _elapsed_ms(step)
        geometry = recorder.build(self.template.page_dimensions, self._block_geometry())

        step = time.perf_counter()
        aligned_image = detailed["aligned_image"]
        if variants:
            zone_results = self.template.read_zones_with_variants(
                aligned_image, variants, self.zone_engines
            )
        else:
            zone_results = read_zones(
                self.template.zones, aligned_image, self.zone_engines
            )
        zone_images = attach_zone_images(zone_results, self.template.zones, file_id)
        timings["zones"] = _elapsed_ms(step)

        omr_response = dict(detailed["omr_response"])
        for name, zone_result in zone_results.items():
            omr_response[name] = zone_result.value
        fields = detailed["field_details"]
        responses = get_concatenated_response(omr_response, self.template, fields)

        zones = {name: zone.to_dict() for name, zone in zone_results.items()}
        checks, validation, rule_review = {}, {}, []
        if self.template.rules:
            step = time.perf_counter()
            checks, validation, rule_review = self.template.rules.apply(
                omr_response,
                responses,
                fields,
                zones,
                read_lazy=lambda name: self._read_lazy_zone(
                    name, aligned_image, variants
                ),
            )
            timings["rules"] = _elapsed_ms(step)

        score, scoring = None, {}
        if self.evaluation_config is not None:
            scoring = scoring_summary(
                evaluate_concatenated_response_detailed(
                    responses, self.evaluation_config, Path(file_id), None
                )
            )
            score = scoring["score"]

        groups = describe_groups(omr_response, self.template, fields)
        review = review_items(fields, zones, rule_review)
        review.extend(group_review_items(groups, review))
        review.extend(self._sheet_review(fields))
        review.extend(detailed.get("sheet_review") or [])
        review.extend(recorder.info.get("review") or [])
        quality = measure_quality(
            source, geometry, min(self.template.bubble_dimensions or [0])
        )
        review.extend(quality_review(quality, self.tuning_config.review_params))
        timings["total"] = _elapsed_ms(started)
        return ScanResult(
            file_id=file_id,
            status=STATUS_NEEDS_REVIEW if review else STATUS_OK,
            responses=responses,
            fields=fields,
            zones=zones,
            review=review,
            checks=checks,
            validation=validation,
            groups=groups,
            score=score,
            scoring=scoring,
            thresholds=detailed["thresholds"],
            timings_ms=timings,
            geometry=geometry,
            quality=quality,
            aligned_image=aligned_image if keep_images else None,
            marked_image=detailed["final_marked"] if keep_images else None,
            print_image=detailed.get("print_image") if keep_images else None,
            zone_images=zone_images,
        )

    def needs_print_image(self):
        """A print-kept copy is worth making: blocks are fitted and dropout is on."""
        if self.template.color_dropout.mode == "grey":
            return False
        if self.tuning_config.review_params.get("min_grid_fit", 0):
            # The registration check needs the printed bubble outlines
            return True
        option = self.image_ops.alignment_option
        if option(self.template, "rectify_on_border", False) or option(
            self.template, "block_perspective", False
        ):
            return True
        return any(
            block.rectify_on_border or getattr(block, "block_perspective", None)
            for block in self.template.field_blocks
        )

    def _block_geometry(self):
        """Fitted (or expected, when failed) corners of every fitted block."""
        blocks = {}
        for block in self.template.field_blocks:
            info = getattr(block, "last_rectification", None)
            if not info:
                continue
            corners = info.get("corners") if info.get("ok") else None
            corners = corners or info.get("expected")
            if corners is None:
                x0, y0 = (float(v) for v in block.origin)
                w, h = (float(v) for v in block.dimensions)
                corners = [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]]
            blocks[block.name] = {
                "corners": corners,
                "expected": info.get("expected"),
                "status": info.get("status") or ("found" if info.get("ok") else "failed"),
                "method": info.get("method") or "border",
                "level": info.get("level"),
                "reason": info.get("reason") or "",
            }
        return blocks

    def _read_lazy_zone(self, name, aligned_image, variants=None):
        zone = next(z for z in self.template.zones if z.name == name)
        if variants and zone.color_dropout is not None:
            return self.template.read_zones_with_variants(
                aligned_image, variants, self.zone_engines, only=[zone]
            )[name].to_dict()
        return read_zone(zone, aligned_image, self.zone_engines).to_dict()

    def _sheet_review(self, field_details):
        """Sheet-level review items (opt-in checks)."""
        minimum = self.tuning_config.review_params.get("min_marked_bubbles", 0)
        if not minimum:
            return []
        marked = sum(
            1
            for details in field_details.values()
            for bubble in details["bubbles"]
            if bubble["marked"]
        )
        if marked >= minimum:
            return []
        return [
            {
                "kind": "sheet",
                "name": "too_few_marks",
                "flags": ["too_few_marks"],
                "marked_bubbles": marked,
                "min_marked_bubbles": minimum,
            }
        ]

    def load_images(self, file_path, pdf_params=None):
        """
        [(name, image)] of an image file or the selected pages of a PDF.

        pdf_params (optional): per-request {"pdf_dpi", "pdf_page"} overriding
        config.json (Scan / New Job screens); None values keep the config's.
        """
        config = self.tuning_config
        overrides = {k: v for k, v in (pdf_params or {}).items() if v is not None}
        if overrides:
            values = config.toDict()
            values["pdf_params"] = {**values.get("pdf_params", {}), **overrides}
            config = DotMap(values, _dynamic=False)
        return ImageUtils.load_omr_image(
            Path(file_path), config, color=self.needs_color
        )

    def scan_path(self, file_path, keep_images=True, pdf_params=None):
        """Read an image or every selected page of a PDF; returns a list of results."""
        file_path = Path(file_path)
        images = self.load_images(file_path, pdf_params)
        if not images:
            return [
                ScanResult(file_path.name, STATUS_ERROR, error="File could not be read")
            ]
        results = []
        for name, image in images:
            try:
                results.append(self.scan(image, name, keep_images))
            except Exception as error:
                logger.error(f"Failed to process '{name}': {error}")
                results.append(ScanResult(name, STATUS_ERROR, error=str(error)))
        return results


def _elapsed_ms(start):
    return round((time.perf_counter() - start) * 1000, 1)
