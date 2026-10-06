# Use all imports relative to root directory
from src.processors.manager import Processor


class ImagePreprocessor(Processor):
    """Base class for an extension that applies some preprocessing to the input image"""

    # Set True when the preprocessor must see the original image resolution; if it
    # is the first preprocessor, the initial resize to processing dimensions is skipped
    needs_full_resolution = False
    # How companion images (colour-dropout variants read by some zones) follow
    # this step: "none" = not geometric, skip; "recorded" = the step calls
    # record_geometry() with its warp; anything else = run apply_filter on them
    geometry = "unknown"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.page_dimensions = None

    def record_geometry(self, transform):
        """Register a pure geometric image -> image function to replay on companions."""
        ops = getattr(self.image_instance_ops, "geometry_ops", None)
        if ops is not None:
            ops.append(transform)

    def apply_filter(self, image, filename):
        """Apply filter to the image and returns modified image"""
        raise NotImplementedError

    @staticmethod
    def exclude_files():
        """Returns a list of file paths that should be excluded from processing"""
        return []
