"""
Watch folders: a job with options.watch keeps reading new files dropped into
its folder until it is stopped.

A new file is handed to the job only once it is complete:
- its size and modified time are the same on two looks at least
  SETTLE_S seconds apart, and it is not still being written to (modified
  less than SETTLE_S ago);
- it opens for reading (Windows refuses while the copying program holds it);
- its end looks like the end of that kind of file (JPEG end marker, PNG IEND,
  PDF %%EOF). A file that stays incomplete for GIVE_UP_S is handed over
  anyway, so a damaged file shows up as an error sheet instead of waiting
  forever.

Files already handed over are remembered with their size and modified time
in jobs/<id>.watch, so a restart does not read them again and a file
replaced with new content is read again as a new sheet.
"""

import os
import threading
import time
from pathlib import Path

from src.logger import logger

DEFAULT_INTERVAL_S = 5
MIN_INTERVAL_S = 1
SETTLE_S = 3
GIVE_UP_S = 120
# Names copy programs and editors use while a file is still arriving
TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".download", ".filepart")


def temporary_name(name):
    lower = name.lower()
    return name.startswith((".", "~$")) or lower.endswith(TEMP_SUFFIXES)


def looks_complete(path):
    """The file's last bytes look like the end of its format (or the format has no end marker)."""
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".pdf"):
        return True
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 1024))
            tail = handle.read()
    except OSError:
        return False
    if suffix in (".jpg", ".jpeg"):
        # Some cameras pad after the end marker
        return tail.rstrip(b"\x00").endswith(b"\xff\xd9")
    if suffix == ".png":
        return b"IEND" in tail[-16:]
    return b"%%EOF" in tail


def opens_for_reading(path):
    try:
        with open(path, "rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def list_inputs(folder, recursive, suffixes):
    """{path: (size, mtime_ns)} of the readable files in folder."""
    found = {}
    stack = [str(folder)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if recursive and not entry.name.startswith("."):
                                stack.append(entry.path)
                            continue
                        if not entry.is_file() or temporary_name(entry.name):
                            continue
                        if os.path.splitext(entry.name)[1].lower() not in suffixes:
                            continue
                        stat = entry.stat()
                    except OSError:
                        continue
                    found[str(Path(entry.path))] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            continue
    return found


class WatchState:
    """What one watching job has seen: handed-over files and files arriving."""

    def __init__(self, record_path, files_path):
        self.record_path = Path(record_path)
        self.seen = {}  # path -> (size, mtime_ns) handed to the job
        self.pending = {}  # path -> {"stat", "since", "first"}
        self.next_look = 0.0
        if self.record_path.exists():
            for line in self.record_path.read_text().splitlines():
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    self.seen[parts[2]] = (int(parts[0]), int(parts[1]))
        # Files the job was created with count as handed over
        known = Path(files_path)
        if known.exists():
            new = []
            for path in known.read_text().splitlines():
                path = str(Path(path))
                if path in self.seen:
                    continue
                try:
                    stat = os.stat(path)
                    self.seen[path] = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    self.seen[path] = (-1, -1)
                new.append(path)
            self.remember(new)

    def remember(self, paths):
        if not paths:
            return
        with open(self.record_path, "a") as handle:
            for path in paths:
                size, mtime = self.seen[path]
                handle.write(f"{size}\t{mtime}\t{path.replace(chr(10), ' ')}\n")

    def ready(self, listing, now):
        """Paths in listing that are new (or changed) and complete."""
        ready = []
        for path, stat in listing.items():
            if self.seen.get(path) == stat:
                self.pending.pop(path, None)
                continue
            entry = self.pending.get(path)
            if entry is None or entry["stat"] != stat:
                # New, or still growing: look again later
                self.pending[path] = {"stat": stat, "since": now, "first": (entry or {}).get("first", now)}
                continue
            if now - entry["since"] < SETTLE_S or stat[0] <= 0:
                continue
            if time.time() - stat[1] / 1e9 < SETTLE_S:
                continue
            complete = opens_for_reading(path) and looks_complete(path)
            if not complete and now - entry["first"] < GIVE_UP_S:
                continue
            if not complete:
                logger.warning(f"Watched file still looks incomplete, reading it anyway: {path}")
            ready.append(path)
        for path in list(self.pending):
            if path not in listing:
                self.pending.pop(path)
        for path in ready:
            self.seen[path] = listing[path]
            self.pending.pop(path, None)
        self.remember(ready)
        return sorted(ready)


class FolderWatcher:
    """One thread that looks at every watching job's folder in turn."""

    def __init__(self, jobs):
        self.jobs = jobs
        self.states = {}  # job_id -> WatchState
        self.lock = threading.Lock()
        self.thread = None

    def add(self, job_id):
        with self.lock:
            self.states.setdefault(job_id, None)

    def discard(self, job_id):
        with self.lock:
            self.states.pop(job_id, None)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="omr-watch", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.jobs.stopping.wait(1):
            try:
                self.tick(time.monotonic())
            except Exception as error:  # keep watching the other folders
                logger.error(f"Folder watch: {error}")

    def tick(self, now):
        with self.lock:
            job_ids = list(self.states)
        for job_id in job_ids:
            job = self.jobs.live.get(job_id) or self.jobs.get(job_id)
            watch = (job or {}).get("options", {}).get("watch") or {}
            if not job or not watch.get("active") or job.get("state") not in self.jobs.WATCH_STATES:
                self.discard(job_id)
                continue
            state = self.states.get(job_id)
            if state is None:
                state = WatchState(
                    self.jobs.data.jobs / f"{job_id}.watch", self.jobs.files_path(job_id)
                )
                with self.lock:
                    if job_id not in self.states:
                        continue
                    self.states[job_id] = state
            if now < state.next_look:
                continue
            interval = max(MIN_INTERVAL_S, float(watch.get("interval_s") or DEFAULT_INTERVAL_S))
            state.next_look = now + interval
            if job.get("state") == self.jobs.PAUSED_STATE:
                continue
            folder = watch.get("folder")
            if not folder or not os.path.isdir(folder):
                continue
            listing = list_inputs(folder, watch.get("recursive", True), self.jobs.INPUT_SUFFIXES)
            ready = state.ready(listing, now)
            self.jobs.watch_found(job_id, ready)
