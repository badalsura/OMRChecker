"""Automatic template generation: see src/template_gen/engine.py."""

from src.template_gen.corrections import apply_corrections, bubble_boxes, render_overlay
from src.template_gen.engine import (
    DEFAULT_OPTIONS,
    GenerationResult,
    generate_template,
    self_check,
    validate_template,
)

__all__ = [
    "DEFAULT_OPTIONS",
    "GenerationResult",
    "apply_corrections",
    "bubble_boxes",
    "generate_template",
    "render_overlay",
    "self_check",
    "validate_template",
]
