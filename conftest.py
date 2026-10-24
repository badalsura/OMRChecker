import os
import time

# Sample snapshots name result files by local hour at the frozen epoch (05AM in IST)
os.environ["TZ"] = "Asia/Kolkata"
time.tzset()

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def no_installed_models(monkeypatch):
    """Tests see only the models they build: PaddleOCR models installed in
    packaging/models (a developer's machine) would change what is read."""
    from src.readers import paddle_ocr

    monkeypatch.setattr(paddle_ocr, "default_model_dirs", lambda: [])
    monkeypatch.delenv(paddle_ocr.MODEL_DIR_ENV, raising=False)
    paddle_ocr.reset_cache()
    yield
    paddle_ocr.reset_cache()
