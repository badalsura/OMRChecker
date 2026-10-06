"""
Library entry point for reading OMR sheets without the CLI's folder conventions.

    engine = OMREngine("samples/sample1/template.json")
    result = engine.scan(cv2.imread("sheet.jpg", cv2.IMREAD_GRAYSCALE), "sheet.jpg")
    result.to_dict()

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
from src.evaluation import EvaluationConfig, evaluate_concatenated_response
from src.logger import logger
from src.ml.classifiers import load_crop_classifier
from src.readers import read_zones
from src.template import Template
from src.utils.image import ImageUtils
from src.utils.parsing import get_concatenated_response, open_config_with_defaults

STATUS_OK = "ok"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_ERROR = "error"


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
    # Images are kept out of to_dict(); callers decide whether to persist them
    aligned_image: Optional[np.ndarray] = None
    marked_image: Optional[np.ndarray] = None

    def to_dict(self):
        return {
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
        }


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

        self.template = Template(template_path, self.tuning_config)
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

        ml_params = self.tuning_config.ml_params
        self.image_ops = self.template.image_instance_ops
        self.image_ops.bubble_classifier = load_crop_classifier(
            resolve_path(template_dir, bubble_model_path or ml_params.bubble_model_path)
        )
        self.zone_engines = {
            "icr": load_crop_classifier(
                resolve_path(template_dir, icr_model_path or ml_params.icr_model_path)
            )
        }

    def scan(self, image, file_id="image", keep_images=True):
        """Read one grayscale sheet image."""
        timings = {}
        started = time.perf_counter()
        if image is None:
            return ScanResult(file_id, STATUS_ERROR, error="Image could not be read")
        if image.ndim == 3:
            import cv2

            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        self.image_ops.reset_all_save_img()
        aligned = self.image_ops.apply_preprocessors(file_id, image, self.template)
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
            self.template, aligned, file_id, save_dir=None
        )
        timings["bubbles"] = _elapsed_ms(step)

        step = time.perf_counter()
        aligned_image = detailed["aligned_image"]
        zone_results = read_zones(self.template.zones, aligned_image, self.zone_engines)
        timings["zones"] = _elapsed_ms(step)

        omr_response = dict(detailed["omr_response"])
        for name, zone_result in zone_results.items():
            omr_response[name] = zone_result.value
        responses = get_concatenated_response(omr_response, self.template)

        score = None
        if self.evaluation_config is not None:
            score = evaluate_concatenated_response(
                responses, self.evaluation_config, Path(file_id), None
            )

        review = [
            {"kind": "field", "name": name, "flags": details["flags"]}
            for name, details in detailed["field_details"].items()
            if details["needs_review"]
        ] + [
            {"kind": "zone", "name": name, "flags": zone.flags}
            for name, zone in zone_results.items()
            if zone.needs_review
        ]
        timings["total"] = _elapsed_ms(started)
        return ScanResult(
            file_id=file_id,
            status=STATUS_NEEDS_REVIEW if review else STATUS_OK,
            responses=responses,
            fields=detailed["field_details"],
            zones={name: zone.to_dict() for name, zone in zone_results.items()},
            review=review,
            score=score,
            thresholds=detailed["thresholds"],
            timings_ms=timings,
            aligned_image=aligned_image if keep_images else None,
            marked_image=detailed["final_marked"] if keep_images else None,
        )

    def scan_path(self, file_path, keep_images=True):
        """Read an image or every selected page of a PDF; returns a list of results."""
        file_path = Path(file_path)
        images = ImageUtils.load_omr_image(file_path, self.tuning_config)
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
