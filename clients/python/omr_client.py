"""
Zero-dependency Python client for the OMR Engine REST API (Python 3.8+).

    from omr_client import OMRClient

    client = OMRClient("http://127.0.0.1:8000", api_key=None)
    template = client.upload_template(["exam/template.json", "exam/evaluation.json"])
    result = client.scan(template["id"], ["sheet1.jpg"])["scans"][0]
    print(result["status"], result["responses"])

    # Bulk: the server reads a folder on its own disk with all its cores
    job = client.create_job(template["id"], folder=r"D:\\scans\\day1")
    job = client.wait_for_job(job["id"], callback=lambda j: print(j["progress"]))
    client.job_results_csv(job["id"], "day1.csv")

Only the standard library is used (urllib + a small multipart encoder), so the
file can be copied into any project. Errors raise OMRApiError with the HTTP
status and the server's JSON detail.
"""

import json
import mimetypes
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union
from urllib import error as urlerror
from urllib import parse, request

__all__ = ["OMRClient", "OMRApiError", "FINAL_JOB_STATES"]
__version__ = "1.0.0"

FINAL_JOB_STATES = ("completed", "failed", "cancelled", "interrupted")

# A file to upload: a path, or (file_name, bytes) / (file_name, bytes, content_type)
FileLike = Union[str, os.PathLike, Tuple[str, bytes], Tuple[str, bytes, str]]


class OMRApiError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


def _guess_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _file_part(item: FileLike) -> Tuple[str, bytes, str]:
    if isinstance(item, tuple):
        name, content = item[0], item[1]
        content_type = item[2] if len(item) > 2 else _guess_type(name)
        return name, content, content_type
    path = Path(item)
    return path.name, path.read_bytes(), _guess_type(path.name)


def encode_multipart(
    fields: Dict[str, Any], files: Sequence[Tuple[str, FileLike]]
) -> Tuple[bytes, str]:
    """Return (body, content_type) for a multipart/form-data request."""
    boundary = "----omr" + uuid.uuid4().hex
    chunks: List[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for field_name, item in files:
        file_name, content, content_type = _file_part(item)
        quoted = file_name.replace('"', "%22").replace("\r", "").replace("\n", "")
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{quoted}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class OMRClient:
    """Thin wrapper over the REST API. Every method returns parsed JSON."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        api_key: Optional[str] = None,
        timeout: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # ------------------------------------------------------------ transport
    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = self.base_url + path
        if params:
            clean = {
                k: ("true" if v is True else "false" if v is False else v)
                for k, v in params.items()
                if v is not None
            }
            if clean:
                url += "?" + parse.urlencode(clean)
        return url

    def _open(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ):
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if content_type:
            headers["Content-Type"] = content_type
        req = request.Request(
            self._url(path, params), data=body, headers=headers, method=method
        )
        try:
            return request.urlopen(req, timeout=self.timeout)
        except urlerror.HTTPError as err:
            raw = err.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
                detail = (
                    payload.get("detail", payload)
                    if isinstance(payload, dict)
                    else payload
                )
            except ValueError:
                payload, detail = raw, raw.decode("utf-8", "replace")
            raise OMRApiError(err.code, str(detail), payload) from None

    def _json(self, method, path, params=None, body=None, content_type=None):
        with self._open(method, path, params, body, content_type) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else None

    def _multipart(self, path, fields=None, files=(), params=None):
        body, content_type = encode_multipart(fields or {}, list(files))
        return self._json("POST", path, params, body, content_type)

    def _post_json(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        return self._json("POST", path, body=body, content_type="application/json")

    # ------------------------------------------------------------ meta
    def health(self) -> Dict[str, Any]:
        return self._json("GET", "/health")

    def capabilities(self) -> Dict[str, Any]:
        return self._json("GET", "/capabilities")

    # ------------------------------------------------------------ templates
    def list_templates(self) -> List[Dict[str, Any]]:
        return self._json("GET", "/templates")["templates"]

    def get_template(self, template_id: str) -> Dict[str, Any]:
        return self._json("GET", f"/templates/{parse.quote(template_id)}")

    def upload_template(
        self, paths: Iterable[FileLike], name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Upload template.json (+ config.json, evaluation.json, marker images) or a .zip."""
        return self._multipart(
            "/templates", {"name": name}, [("files", p) for p in paths]
        )

    def update_template(
        self, template_id: str, template: Dict[str, Any]
    ) -> Dict[str, Any]:
        body = json.dumps({"template": template}).encode("utf-8")
        return self._json(
            "PUT",
            f"/templates/{parse.quote(template_id)}",
            body=body,
            content_type="application/json",
        )

    def delete_template(self, template_id: str, purge_scans: bool = False):
        return self._json(
            "DELETE",
            f"/templates/{parse.quote(template_id)}",
            {"purge_scans": purge_scans},
        )

    def generate_template(
        self,
        sample_images: Iterable[FileLike],
        labels_csv: Optional[FileLike] = None,
        name: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Draft a template from ~20 labelled sample sheets (verify it in the GUI)."""
        files = [("files", p) for p in sample_images]
        if labels_csv is not None:
            files.append(("labels", labels_csv))
        fields = {"name": name, "options": json.dumps(options) if options else None}
        return self._multipart("/templates/generate", fields, files)

    # ------------------------------------------------------------ scans
    def scan(
        self, template_id: str, files: Iterable[FileLike], save_images: str = "all"
    ) -> Dict[str, Any]:
        """Read a few sheets synchronously: returns {"scans": [result, ...]}."""
        return self._multipart(
            "/scans",
            {"template_id": template_id, "save_images": save_images},
            [("files", f) for f in files],
        )

    def get_scan(self, scan_id: str) -> Dict[str, Any]:
        return self._json("GET", f"/scans/{parse.quote(scan_id)}")

    def list_scans(self, **filters) -> Dict[str, Any]:
        """filters: status, template_id, job_id, reviewed, limit, offset."""
        return self._json("GET", "/scans", filters)

    def scan_image(self, scan_id: str, kind: str = "marked") -> bytes:
        with self._open(
            "GET", f"/scans/{parse.quote(scan_id)}/image", {"kind": kind}
        ) as r:
            return r.read()

    def scan_crop(self, scan_id: str, name: str, pad: int = 20) -> bytes:
        with self._open(
            "GET", f"/scans/{parse.quote(scan_id)}/crop", {"name": name, "pad": pad}
        ) as r:
            return r.read()

    # ------------------------------------------------------------ jobs
    def create_job(
        self,
        template_id: str,
        files: Optional[Iterable[FileLike]] = None,
        folder: Optional[str] = None,
        workers: Optional[int] = None,
        start: bool = True,
        recursive: bool = True,
        save_images: str = "review",
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a bulk job from uploaded files and/or a folder on the server's disk.

        For very large uploads pass start=False, add files in chunks with
        upload_job_files(), then call start_job().
        """
        fields = {
            "template_id": template_id,
            "folder": str(folder) if folder is not None else None,
            "recursive": recursive,
            "save_images": save_images,
            "workers": workers,
            "name": name,
            "start": start,
        }
        return self._multipart("/jobs", fields, [("files", f) for f in files or []])

    def upload_job_files(
        self, job_id: str, files: Iterable[FileLike]
    ) -> Dict[str, Any]:
        return self._multipart(
            f"/jobs/{parse.quote(job_id)}/files", {}, [("files", f) for f in files]
        )

    def upload_job_files_chunked(
        self, job_id: str, paths: Sequence[FileLike], chunk_size: int = 50
    ) -> Dict[str, Any]:
        job = None
        for start in range(0, len(paths), chunk_size):
            job = self.upload_job_files(job_id, paths[start : start + chunk_size])
        return job or self.job_status(job_id)

    def start_job(self, job_id: str) -> Dict[str, Any]:
        return self._json("POST", f"/jobs/{parse.quote(job_id)}/start")

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        return self._json("POST", f"/jobs/{parse.quote(job_id)}/cancel")

    def job_status(self, job_id: str) -> Dict[str, Any]:
        return self._json("GET", f"/jobs/{parse.quote(job_id)}")

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._json("GET", "/jobs", {"limit": limit})["jobs"]

    def wait_for_job(
        self,
        job_id: str,
        poll: float = 2.0,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Poll until the job reaches a final state; callback(job) after every poll."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            job = self.job_status(job_id)
            if callback is not None:
                callback(job)
            if job.get("state") in FINAL_JOB_STATES:
                return job
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"Job {job_id} is still {job.get('state')}")
            time.sleep(poll)

    def job_results_csv(self, job_id: str, out_path: Union[str, os.PathLike]) -> Path:
        """Stream the job's CSV (one row per page) to out_path."""
        out_path = Path(out_path)
        with self._open("GET", f"/jobs/{parse.quote(job_id)}/results.csv") as response:
            with open(out_path, "wb") as handle:
                shutil.copyfileobj(response, handle, 1024 * 1024)
        return out_path

    # ------------------------------------------------------------ review
    def review_queue(
        self,
        template_id: Optional[str] = None,
        job_id: Optional[str] = None,
        scan_id: Optional[str] = None,
        name: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Uncertain reads (low confidence, double marks, errors) waiting for a human."""
        return self._json(
            "GET",
            "/review",
            {
                "template_id": template_id,
                "job_id": job_id,
                "scan_id": scan_id,
                "name": name,
                "kind": kind,
                "limit": limit,
                "offset": offset,
            },
        )

    def review_summary(self, template_id=None, job_id=None) -> Dict[str, Any]:
        return self._json(
            "GET", "/review/summary", {"template_id": template_id, "job_id": job_id}
        )

    def submit_review(
        self,
        scan_id: str,
        corrections: Optional[Dict[str, str]] = None,
        accept: Optional[Iterable[str]] = None,
        reviewer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """corrections: {name: value}; accept: names whose read value is right."""
        return self._post_json(
            f"/scans/{parse.quote(scan_id)}/review",
            {
                "corrections": dict(corrections or {}),
                "accept": list(accept or []),
                "reviewer": reviewer,
            },
        )
