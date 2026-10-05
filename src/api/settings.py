"""Runtime settings, read from the environment with explicit overrides."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    data_dir: Path
    # Optional shared secret, sent by clients in the X-API-Key header
    api_key: Optional[str] = None
    # Worker processes for bulk jobs (0 = one per CPU core)
    workers: int = 0
    # Per uploaded file limit
    max_upload_mb: int = 100
    # Files accepted by the synchronous POST /scans endpoint per request
    sync_max_files: int = 20
    # Server-side folders a job may read from; empty = any folder
    allowed_dirs: List[Path] = field(default_factory=list)
    # "spawn" is the safe choice inside a threaded web server
    mp_start_method: str = "spawn"
    # Restart jobs that were interrupted by a server restart
    resume_jobs: bool = True
    # Origins allowed to call the API from a browser (CORS); empty = none
    cors_origins: List[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, data_dir=None, **overrides):
        data_dir = Path(data_dir or os.environ.get("OMR_DATA_DIR", "./omr_data"))
        allowed = [
            Path(p).resolve()
            for p in os.environ.get("OMR_ALLOWED_DIRS", "").split(os.pathsep)
            if p.strip()
        ]
        settings = cls(
            data_dir=data_dir.resolve(),
            api_key=os.environ.get("OMR_API_KEY") or None,
            workers=_env_int("OMR_WORKERS", 0),
            max_upload_mb=_env_int("OMR_MAX_UPLOAD_MB", 100),
            sync_max_files=_env_int("OMR_SYNC_MAX_FILES", 20),
            allowed_dirs=allowed,
            mp_start_method=os.environ.get("OMR_MP_START", "spawn"),
            resume_jobs=os.environ.get("OMR_RESUME_JOBS", "1") not in ("0", "false"),
            cors_origins=[
                o.strip()
                for o in os.environ.get("OMR_CORS_ORIGINS", "").split(",")
                if o.strip()
            ],
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(settings, key, value)
        return settings

    @property
    def effective_workers(self):
        return self.workers if self.workers > 0 else (os.cpu_count() or 1)
