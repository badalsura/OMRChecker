import cv2
import numpy as np
import pytest

from src.template_gen import printed_labels
from src.template_gen.bubbles import Grid


def printed_grid(values, fields, horizontal=True, size=34, pitch=50):
    image = np.full((80 + pitch * max(len(values), fields), 80 + pitch * max(len(values), fields)), 255, np.uint8)
    for f in range(fields):
        for v, text in enumerate(values):
            cx, cy = (40 + v * pitch, 40 + f * pitch) if horizontal else (40 + f * pitch, 40 + v * pitch)
            cv2.circle(image, (cx, cy), size // 2, 0, 2, cv2.LINE_AA)
            scale = cv2.getFontScaleFromHeight(cv2.FONT_HERSHEY_SIMPLEX, 14, 2)
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
            cv2.putText(image, text, (cx - tw // 2, cy + th // 2), cv2.FONT_HERSHEY_SIMPLEX, scale, 0, 2, cv2.LINE_AA)
    cols, rows = (len(values), fields) if horizontal else (fields, len(values))
    grid = Grid(x0=40, y0=40, dx=pitch, dy=pitch, cols=cols, rows=rows, bubble=[size, size])
    return image, grid


@pytest.mark.skipif(not printed_labels.available(), reason="Tesseract not installed")
@pytest.mark.parametrize(
    "values,horizontal",
    [(list("ABCD"), True), (list("1234567890"), False)],
)
def test_bubble_values_are_read_from_the_print(values, horizontal):
    image, grid = printed_grid(values, 4, horizontal)
    direction = "horizontal" if horizontal else "vertical"
    assert printed_labels.read_values(image, grid, direction) == values


@pytest.mark.skipif(not printed_labels.available(), reason="Tesseract not installed")
def test_empty_bubbles_are_not_guessed():
    image, grid = printed_grid([" "] * 4, 3)
    assert printed_labels.read_values(image, grid, "horizontal") is None
