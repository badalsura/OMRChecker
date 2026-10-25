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

    preprocess = "resize"

    def prepare(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        """Resize and scale crops into an (N, 1, H, W) float32 batch in [0, 1]."""
        if self.preprocess == "centered_digit":
            return centered_digit_batch(crops, self.input_size)
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
        # "centered_digit": MNIST-style input (see centered_digit)
        self.preprocess = metadata.get("preprocess", "resize")
        self.metadata = metadata
        options = ort.SessionOptions()
        if threads is None:
            # Worker processes set OMR_ONNX_THREADS=1 so N processes don't each
            # start a thread per core (0 = onnxruntime default: all cores)
            threads = int(os.environ.get("OMR_ONNX_THREADS", "0") or 0)
        if threads:
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
        # CPU only: the models are tiny (32x32 crops), and on a Mac
        # get_available_providers() lists CoreML first, which would compile a
        # CoreML copy in every worker process and run slower than the CPU
        self.session = ort.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

    def predict_proba(self, crops):
        if len(crops) == 0:
            return np.zeros((0, len(self.labels)), dtype=np.float32)
        logits = self.session.run(None, {self.input_name: self.prepare(crops)})[0]
        return softmax(logits, self.temperature)


def centered_digit(crop, size=28):
    """
    A cleaned character box (black ink on white) as MNIST/EMNIST models expect
    it: white ink on black, specks dropped, the ink fitted in the middle
    (20 x 20 in 28 x 28) and moved so its centre of mass is the image centre,
    scaled to [0, 1].
    All black when the box has no ink.
    """
    fit = round(size * 20 / 28)
    canvas = np.zeros((size, size), np.float32)
    if crop is None or crop.size == 0:
        return canvas
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(255 - crop, 127, 255, cv2.THRESH_BINARY)
    count, components, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if count <= 1:
        return canvas
    areas = stats[1:, cv2.CC_STAT_AREA]
    keep = [i + 1 for i, area in enumerate(areas) if area >= max(4, 0.12 * areas.max())]
    mask = np.isin(components, keep).astype(np.uint8) * 255
    ys, xs = np.nonzero(mask)
    digit = mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    h, w = digit.shape
    scale = float(fit) / max(h, w)
    nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
    digit = cv2.resize(digit, (nw, nh), interpolation=cv2.INTER_AREA)
    y0, x0 = (size - nh) // 2, (size - nw) // 2
    canvas[y0 : y0 + nh, x0 : x0 + nw] = digit
    total = canvas.sum()
    if total > 0:
        yy, xx = np.indices(canvas.shape)
        cy, cx = (yy * canvas).sum() / total, (xx * canvas).sum() / total
        shift = np.float32([[1, 0, size / 2 - cx], [0, 1, size / 2 - cy]])
        canvas = cv2.warpAffine(
            canvas, shift, (size, size), flags=cv2.INTER_LINEAR, borderValue=0
        )
    return canvas / 255.0


def centered_digit_batch(crops, input_size):
    width, height = input_size
    batch = np.zeros((len(crops), 1, height, width), dtype=np.float32)
    for i, crop in enumerate(crops):
        batch[i, 0] = centered_digit(crop, size=width)
    return batch


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
