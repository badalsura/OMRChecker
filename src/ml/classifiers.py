"""
Crop classifiers used by the OMR and ICR readers.

A classifier takes a list of grayscale crops (uint8, any size) and returns class
probabilities. Models are trained offline (see src/ml/train.py) and exported to
ONNX together with a JSON sidecar that describes the labels and input size:

    bubble_model.onnx
    bubble_model.json  ->  {"labels": ["empty", "marked"], "input_size": [32, 32],
                            "temperature": 1.3, ...}

"temperature" (optional, default 1.0) is the calibration temperature fitted on
the validation set by src/ml/train.py; logits are divided by it before the
softmax so that confidences match observed accuracy.

No trained weights ship with the repository; until a model is configured the
classical threshold readers are used.
"""

import json
import os
from pathlib import Path
from typing import List, Optional, Sequence

import cv2
import numpy as np


class CropClassifier:
    """Base interface: subclasses implement predict_proba."""

    labels: List[str] = []
    input_size = (32, 32)
    temperature = 1.0

    def predict_proba(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        raise NotImplementedError

    def prepare(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        """Resize and scale crops into an (N, 1, H, W) float32 batch in [0, 1]."""
        width, height = self.input_size
        batch = np.zeros((len(crops), 1, height, width), dtype=np.float32)
        for i, crop in enumerate(crops):
            if crop is None or crop.size == 0:
                continue
            if crop.ndim == 3:
                crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            resized = cv2.resize(crop, (width, height), interpolation=cv2.INTER_AREA)
            batch[i, 0] = resized.astype(np.float32) / 255.0
        return batch

    def label_index(self, label: str) -> int:
        return self.labels.index(label)


class OnnxCropClassifier(CropClassifier):
    def __init__(self, model_path, metadata_path: Optional[str] = None, threads=None):
        import onnxruntime as ort

        model_path = Path(model_path)
        metadata_path = (
            Path(metadata_path) if metadata_path else model_path.with_suffix(".json")
        )
        metadata = json.loads(metadata_path.read_text())
        self.labels = list(metadata["labels"])
        self.input_size = tuple(metadata.get("input_size", [32, 32]))
        self.temperature = float(metadata.get("temperature") or 1.0)
        self.metadata = metadata
        options = ort.SessionOptions()
        if threads is None:
            # Worker processes set OMR_ONNX_THREADS=1 so N processes don't each
            # start a thread per core (0 = onnxruntime default: all cores)
            threads = int(os.environ.get("OMR_ONNX_THREADS", "0") or 0)
        if threads:
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path), options, providers=ort.get_available_providers()
        )
        self.input_name = self.session.get_inputs()[0].name

    def predict_proba(self, crops):
        if len(crops) == 0:
            return np.zeros((0, len(self.labels)), dtype=np.float32)
        logits = self.session.run(None, {self.input_name: self.prepare(crops)})[0]
        return softmax(logits, self.temperature)


def softmax(logits, temperature=1.0):
    if temperature and temperature != 1.0:
        logits = logits / temperature
    shifted = logits - logits.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=1, keepdims=True)


def load_crop_classifier(model_path, metadata_path=None):
    if not model_path:
        return None
    return OnnxCropClassifier(model_path, metadata_path)
