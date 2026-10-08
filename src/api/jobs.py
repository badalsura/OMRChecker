"""
Bulk jobs: a single runner thread processes queued jobs one after another and
spreads the files of the running job over a shared process pool. Results are
written to disk by the workers and indexed in SQLite in batches.

A job is resumable: its file list is stored next to its JSON, and on restart
the runner skips files that already have results.
"""

import multiprocessing
import queue
import threading
import time
from collections import deque
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path

from src.api.storage import new_id, read_json, write_json_atomic
from src.api.worker import (
    SAVE_REVIEW,
    archive_template_version,
    get_process_engine,
    job_task,
    scan_and_store,
    summarize,
    template_version,
    worker_init,
)
from src.logger import logger

INPUT_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".pdf"}

QUEUED, UPLOADING, RUNNING = "queued", "uploading", "running"
COMPLETED, FAILED, CANCELLED, INTERRUPTED = (
    "completed",
    "failed",
    "cancelled",
    "interrupted",
)
# Stopped on request; sheets read so far are kept and Resume reads the rest
PAUSED = "paused"
# Files read ahead of the workers by default (a few MB of scans in memory)
DEFAULT_PREFETCH = 64
PREFETCH_READERS = 4
ACTIVE_STATES = {QUEUED, RUNNING}


def _read_bytes(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None  # the worker reads the path itself and reports the error


def read_ahead(tasks, reader, depth):
    """
    Yield the tasks with each file's bytes attached, reading up to depth
    files ahead on background threads so the workers never wait on the disk.
    """
    pending = deque()
    tasks = iter(tasks)
    while True:
        while len(pending) < depth:
            task = next(tasks, None)
            if task is None:
                break
            pending.append((task, reader.submit(_read_bytes, task["file_path"])))
        if not pending:
            return
        task, future = pending.popleft()
        data = future.result()
        yield {**task, "file_bytes": data} if data is not None else task


def collect_folder(folder, recursive=True):
    folder = Path(folder)
    pattern = "**/*" if recursive else "*"
    return sorted(
        p
        for p in folder.glob(pattern)
        if p.is_file() and p.suffix.lower() in INPUT_SUFFIXES
    )


class JobManager:
    def __init__(self, data, index, templates, settings):
        self.data = data
        self.index = index
        self.templates = templates
        self.settings = settings
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.live = {}  # job_id -> job dict while queued/running
        self.cancelled = set()
        self.paused = set()
        self.resume_after = set()
        self.pool = None
        self.pool_workers = None
        self.thread = None
        self.stopping = threading.Event()

    # ---- lifecycle -----------------------------------------------------
    def start(self):
        self.thread = threading.Thread(
            target=self._runner, name="omr-jobs", daemon=True
        )
        self.thread.start()
        for job_file in sorted(self.data.jobs.glob("*.json")):
            job = read_json(job_file)
            if not job or job.get("state") not in (QUEUED, RUNNING, INTERRUPTED):
                continue
            if self.settings.resume_jobs:
                job["state"] = QUEUED
                self._save(job)
                self.live[job["id"]] = job
                self.queue.put(job["id"])
            else:
                job["state"] = INTERRUPTED
                self._save(job)

    def stop(self):
        self.stopping.set()
        self.queue.put(None)
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pool = None
        if self.thread is not None:
            self.thread.join(timeout=10)

    def _get_pool(self, workers):
        # Jobs run one at a time, so a job asking for a different worker count
        # gets a fresh pool once the previous job's pool has drained
        if self.pool is not None and self.pool_workers != workers:
            self.pool.shutdown(wait=True)
            self.pool = None
        if self.pool is None:
            self.pool_workers = workers
            from src.utils.cpu import prepare_worker_environment

            # Spawned workers copy the parent's environment before any
            # initializer runs: thread limits must be set here first
            prepare_worker_environment()
            context = multiprocessing.get_context(self.settings.mp_start_method)
            self.pool = ProcessPoolExecutor(
                max_workers=workers, mp_context=context, initializer=worker_init
            )
        return self.pool

    # ---- API -----------------------------------------------------------
    def create(self, template_id, files, source, options=None, start=True):
        job_id = new_id()
        options = dict(options or {})
        job = {
            "id": job_id,
            "template_id": template_id,
            "source": source,
            "state": QUEUED if start else UPLOADING,
            "options": {
                "save_images": options.get("save_images") or SAVE_REVIEW,
                "workers": options.get("workers"),
                # Files read into memory ahead of the workers (0 = off)
                "prefetch": DEFAULT_PREFETCH
                if options.get("prefetch") is None
                else max(0, int(options["prefetch"])),
                # Per-job PDF rendering {"pdf_dpi", "pdf_page"}; None = config.json
                "pdf_params": options.get("pdf_params"),
            },
            "name": options.get("name") or "",
            # Folder prefix rewrites used when re-reading this job's files later
            "path_remap": list(options.get("path_remap") or []),
            "total_files": 0,
            "processed_files": 0,
            "pages": 0,
            "counts": {},
            "errors": [],
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "throughput_per_s": None,
        }
        self._save(job)
        self.add_files(job, files)
        if start:
            self.enqueue(job)
        else:
            self.live[job_id] = job
        return job

    def inputs_dir(self, job_id):
        path = self.data.jobs / job_id / "inputs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def files_path(self, job_id):
        return self.data.jobs / f"{job_id}.files"

    def add_files(self, job, files):
        files = [str(Path(f)) for f in files]
        if not files:
            return job
        with self.lock:
            with open(self.files_path(job["id"]), "a") as handle:
                for path in files:
                    handle.write(path.replace("\n", " ") + "\n")
            job["total_files"] = job.get("total_files", 0) + len(files)
            self._save(job)
        return job

    def enqueue(self, job):
        job["state"] = QUEUED
        self._save(job)
        self.live[job["id"]] = job
        self.queue.put(job["id"])

    def get(self, job_id):
        if job_id in self.live:
            return dict(self.live[job_id])
        if "/" in job_id or not job_id.isalnum():
            return None
        return read_json(self.data.job_file(job_id))

    def list(self, limit=50):
        jobs = []
        for job_file in sorted(
            self.data.jobs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:limit]:
            job = self.live.get(job_file.stem) or read_json(job_file)
            if job:
                jobs.append(job)
        return jobs

    def pause(self, job_id):
        """Stop handing out files; sheets being read finish, then the job pauses."""
        job = self.live.get(job_id)
        if job is None or job["state"] not in (QUEUED, RUNNING):
            return None
        self.paused.add(job_id)
        if job["state"] == QUEUED:
            job["state"] = PAUSED
            self._save(job)
            self.live.pop(job_id, None)
            self.paused.discard(job_id)
        return job

    def resume(self, job_id):
        """Queue a paused or interrupted job again; it skips sheets already read."""
        live = self.live.get(job_id)
        if live is not None:
            if live["state"] != PAUSED:
                return None
            # Still winding down: queue it again once the runner lets go
            self.resume_after.add(job_id)
            return live
        job = self.get(job_id)
        if job is None or job.get("state") not in (PAUSED, INTERRUPTED):
            return None
        self.enqueue(job)
        return job

    def cancel(self, job_id):
        job = self.live.get(job_id)
        if job is not None and job["state"] == PAUSED:
            # Paused but the runner has not let go yet
            self.resume_after.discard(job_id)
            job["state"] = CANCELLED
            job["finished_at"] = time.time()
            self._save(job)
            return job
        if job is None:
            job = self.get(job_id)
            if job is not None and job.get("state") in (PAUSED, INTERRUPTED):
                job["state"] = CANCELLED
                job["finished_at"] = time.time()
                self._save(job)
            return job
        self.cancelled.add(job_id)
        if job["state"] in (QUEUED, UPLOADING):
            job["state"] = CANCELLED
            job["finished_at"] = time.time()
            self._save(job)
            self.live.pop(job_id, None)
        return job

    def update(self, job_id, changes):
        """Edit settings of a job (name, path_remap) at any time."""
        with self.lock:
            job = self.live.get(job_id)
            if job is None:
                job = self.get(job_id)
                if job is None:
                    return None
            job.update(changes)
            self._save(job)
            return dict(job)

    def _save(self, job):
        write_json_atomic(self.data.job_file(job["id"]), job)

    # ---- runner --------------------------------------------------------
    def _runner(self):
        while not self.stopping.is_set():
            job_id = self.queue.get()
            if job_id is None:
                return
            job = self.live.get(job_id)
            if job is None or job["state"] != QUEUED:
                continue
            try:
                self._run(job)
            except Exception as error:  # keep the runner alive
                logger.error(f"Job {job_id} failed: {error}")
                job["state"] = FAILED
                job["errors"].append(str(error))
            finally:
                if job["state"] == RUNNING:
                    job["state"] = INTERRUPTED if self.stopping.is_set() else COMPLETED
                job["finished_at"] = time.time()
                self._refresh_counts(job)
                self._save(job)
                self.live.pop(job_id, None)
                self.cancelled.discard(job_id)
                self.paused.discard(job_id)
                if job_id in self.resume_after:
                    self.resume_after.discard(job_id)
                    if job["state"] == PAUSED:
                        self.enqueue(job)

    def _halted(self, job):
        return (
            job["id"] in self.cancelled
            or job["id"] in self.paused
            or self.stopping.is_set()
        )

    def _refresh_counts(self, job):
        job["counts"] = self.index.status_counts(job_id=job["id"])
        job["pages"] = sum(job["counts"].values())

    def _run(self, job):
        template_id = job["template_id"]
        template_dir = self.templates.path(template_id)
        if not (template_dir / "template.json").exists():
            raise RuntimeError(f"Template '{template_id}' no longer exists")
        version = template_version(template_dir)
        paths = Path(self.files_path(job["id"])).read_text().splitlines()
        done = self.index.job_seqs(job["id"])
        job["total_files"] = len(paths)
        job["processed_files"] = len(done)
        job["state"] = RUNNING
        job["started_at"] = job.get("started_at") or time.time()
        self._refresh_counts(job)
        self._save(job)
        base = {
            "template_id": template_id,
            "template_dir": str(template_dir),
            "version": version,
            # Content hash recorded with every scan; the folder is archived so
            # results can be re-rendered after the template is edited
            "template_version": archive_template_version(
                template_dir, self.data.template_versions, template_id
            ),
            "job_id": job["id"],
            "scans_root": str(self.data.scans),
            "save_images": job["options"]["save_images"],
            "pdf_params": job["options"].get("pdf_params"),
        }
        tasks = (
            {**base, "seq": seq, "file_path": path, "file_name": Path(path).name}
            for seq, path in enumerate(paths)
            if seq not in done
        )
        workers = job["options"].get("workers") or self.settings.effective_workers
        run_started, processed_now = time.time(), 0
        pending_rows, last_flush = [], time.time()

        def flush(force=False):
            nonlocal pending_rows, last_flush
            if pending_rows and (
                force or len(pending_rows) >= 200 or time.time() - last_flush > 1
            ):
                self.index.add_scans(pending_rows)
                pending_rows = []
            if force or time.time() - last_flush > 1:
                elapsed = max(time.time() - run_started, 1e-6)
                job["throughput_per_s"] = round(processed_now / elapsed, 2)
                remaining = job["total_files"] - job["processed_files"]
                rate = job["throughput_per_s"] or 0
                job["eta_s"] = round(remaining / rate) if rate > 0 else None
                self._refresh_counts(job)
                self._save(job)
                last_flush = time.time()

        def record(summaries, task):
            nonlocal processed_now
            pending_rows.extend(summaries)
            processed_now += 1
            job["processed_files"] += 1
            for summary in summaries:
                if summary.get("status") == "error" and len(job["errors"]) < 100:
                    job["errors"].append(f"{task['file_name']}: {summary.get('error')}")
            flush()

        if workers <= 1:
            engine = get_process_engine(base["template_dir"], version)
            for task in tasks:
                if self._halted(job):
                    break
                try:
                    stored = scan_and_store(
                        engine,
                        task["file_path"],
                        task,
                        task["scans_root"],
                        task["save_images"],
                        copy_input=False,
                    )
                    summaries = [summarize(r) for r in stored]
                except Exception as error:
                    summaries = []
                    job["errors"].append(f"{task['file_name']}: {error}")
                record(summaries, task)
        else:
            pool = self._get_pool(workers)
            in_flight = {}
            max_in_flight = workers * 4
            prefetch = int(job["options"].get("prefetch") or 0)
            reader = None
            if prefetch:
                reader = ThreadPoolExecutor(
                    max_workers=PREFETCH_READERS, thread_name_prefix="omr-prefetch"
                )
                tasks = read_ahead(tasks, reader, prefetch)
            tasks_iter = iter(tasks)
            exhausted = False
            while True:
                stop = self._halted(job)
                while not stop and not exhausted and len(in_flight) < max_in_flight:
                    task = next(tasks_iter, None)
                    if task is None:
                        exhausted = True
                        break
                    in_flight[pool.submit(job_task, task)] = task
                if not in_flight:
                    break
                finished, _ = wait(
                    list(in_flight), timeout=1, return_when=FIRST_COMPLETED
                )
                for future in finished:
                    task = in_flight.pop(future)
                    try:
                        summaries = future.result()
                    except Exception as error:
                        summaries = []
                        job["errors"].append(f"{task['file_name']}: {error}")
                    record(summaries, task)
                if stop:
                    for future in list(in_flight):
                        if future.cancel():
                            in_flight.pop(future)
            if reader is not None:
                reader.shutdown(wait=False, cancel_futures=True)
        flush(force=True)
        if job["id"] in self.cancelled:
            job["state"] = CANCELLED
        elif job["id"] in self.paused and job["processed_files"] < job["total_files"]:
            job["state"] = PAUSED
