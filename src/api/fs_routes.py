"""
Server folder picker for the New Job screen (plan item 11).

    GET /fs/roots            drives (Windows) or / and home, or the allowed folders
    GET /fs/browse?path=     sub-folders with their image / PDF counts
    GET /fs/check?path=      "does this folder exist, may I read it, what is in it"
    GET /fs/recent           the last 10 folders jobs were started from

Who sees what (folder_scope): administrators, the API key and a station
without accounts may use every folder; any other signed-in user only the
folders an administrator allowed for that account (none: upload only). When
OMR_ALLOWED_DIRS is set it limits everyone, so a shared server never shows the
rest of its disks.
"""

import os
import string
import sys
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Query, Request

from src.api.jobs import INPUT_SUFFIXES
from src.api.storage import read_json, write_json_atomic

RECENT_LIMIT = 10
MAX_FOLDERS = 1000
COUNT_LIMIT = 5000  # entries looked at per folder in a listing
CHECK_LIMIT = 200000  # files looked at by a (recursive) check
IMAGE_SUFFIXES = INPUT_SUFFIXES - {".pdf"}


class FolderError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _resolve(path):
    try:
        return Path(os.path.expanduser(str(path).strip())).resolve()
    except (OSError, RuntimeError, ValueError):
        raise FolderError("Not a valid folder path", 400) from None


def _inside(path, roots):
    return any(path == root or root in path.parents for root in roots)


def folder_scope(settings, request=None):
    """
    The folders a request may use: None means any folder. Administrators, the
    API key and a station without accounts get OMR_ALLOWED_DIRS (or anything);
    another signed-in user gets the folders allowed for the account, kept
    inside OMR_ALLOWED_DIRS.
    """
    station = list(settings.allowed_dirs) or None
    user = getattr(request.state, "user", None) if request is not None else None
    if not user or user.get("role") == "admin":
        return station
    mine = []
    for folder in user.get("folders") or []:
        try:
            mine.append(_resolve(folder))
        except FolderError:
            continue
    if station is None:
        return mine
    # Where the two overlap: a user folder inside the station's, or the reverse
    return [p for p in mine if _inside(p, station)] + [
        s for s in station if any(p in s.parents for p in mine)
    ]


def is_allowed(scope, path):
    return scope is None or _inside(path, scope)


def checked_folder(scope, raw):
    """Resolved Path of an existing, allowed folder, or FolderError."""
    if not raw or not str(raw).strip():
        raise FolderError("Enter a folder path", 400)
    path = _resolve(raw)
    if not is_allowed(scope, path):
        raise FolderError("Outside the allowed folders", 403)
    if not path.exists():
        raise FolderError("Folder not found", 404)
    if not path.is_dir():
        raise FolderError("That is a file, not a folder", 400)
    return path


def windows_drives():
    drives = []
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for index, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << index):
                drives.append(f"{letter}:\\")
    except Exception:  # not Windows, or ctypes unavailable
        drives = [
            f"{letter}:\\"
            for letter in string.ascii_uppercase[2:]
            if os.path.exists(f"{letter}:\\")
        ]
    return drives


def roots(scope):
    if scope is not None:
        return [
            {"path": str(root), "name": str(root), "kind": "allowed"}
            for root in scope
            if root.is_dir()
        ]
    if sys.platform.startswith("win"):
        return [{"path": d, "name": d, "kind": "drive"} for d in windows_drives()]
    items = [{"path": "/", "name": "/", "kind": "drive"}]
    home = Path.home()
    if home.is_dir() and str(home) != "/":
        items.append({"path": str(home), "name": f"Home ({home})", "kind": "home"})
    return items


def count_inputs(folder, limit=COUNT_LIMIT, recursive=False):
    """{"images", "pdfs", "truncated"} for the readable files in a folder."""
    images = pdfs = seen = 0
    stack = [str(folder)]
    truncated = False
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > limit:
                        truncated = True
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if recursive and not entry.name.startswith("."):
                                stack.append(entry.path)
                            continue
                        if not entry.is_file():
                            continue
                    except OSError:
                        continue
                    suffix = os.path.splitext(entry.name)[1].lower()
                    if suffix == ".pdf":
                        pdfs += 1
                    elif suffix in IMAGE_SUFFIXES:
                        images += 1
        except OSError:
            continue
        if truncated:
            break
    return {"images": images, "pdfs": pdfs, "truncated": truncated}


def browse(scope, raw):
    folder = checked_folder(scope, raw)
    folders = []
    try:
        with os.scandir(folder) as entries:
            children = []
            for entry in entries:
                try:
                    if entry.is_dir() and not entry.name.startswith((".", "$")):
                        children.append(entry)
                except OSError:
                    continue
    except PermissionError:
        raise FolderError("No permission to read this folder", 403) from None
    except OSError as error:
        raise FolderError(f"Cannot read this folder: {error}", 400) from None
    children.sort(key=lambda e: e.name.lower())
    for entry in children[:MAX_FOLDERS]:
        counts = count_inputs(entry.path)
        folders.append({"name": entry.name, "path": entry.path, **counts})
    parent = folder.parent
    return {
        "path": str(folder),
        "parent": str(parent)
        if parent != folder and is_allowed(scope, parent)
        else None,
        "folders": folders,
        "more_folders": max(0, len(children) - MAX_FOLDERS),
        **count_inputs(folder),
    }


def check(scope, raw, recursive=True):
    try:
        folder = checked_folder(scope, raw)
    except FolderError as error:
        return {"ok": False, "path": raw, "error": str(error)}
    counts = count_inputs(folder, CHECK_LIMIT, recursive)
    result = {"ok": True, "path": str(folder), "recursive": recursive, **counts}
    if not counts["images"] and not counts["pdfs"]:
        result["ok"] = False
        result["error"] = "No images or PDFs in this folder" + (
            " or its subfolders" if recursive else ""
        )
    return result


def recent(ctx, scope=None):
    stored = read_json(ctx.data.settings_file, {}) or {}
    folders = [str(p) for p in stored.get("recent_folders") or []][:RECENT_LIMIT]
    return [f for f in folders if is_allowed(scope, Path(f))]


def remember_folder(ctx, folder):
    """Put a folder a job was started from at the top of the recent list."""
    try:
        folder = str(_resolve(folder))
        stored = read_json(ctx.data.settings_file, {}) or {}
        folders = [f for f in stored.get("recent_folders") or [] if f != folder]
        stored["recent_folders"] = [folder] + folders[: RECENT_LIMIT - 1]
        write_json_atomic(ctx.data.settings_file, stored)
    except Exception:  # never fail a job over the recent list
        pass


def register(app, ctx, secured):
    settings = ctx.settings

    def fail(error):
        raise HTTPException(error.status, str(error)) from None

    @app.get("/fs/roots", tags=["folders"], dependencies=secured)
    def fs_roots(request: Request):
        """Starting points of the folder picker: drives, or the allowed folders."""
        scope = folder_scope(settings, request)
        user = getattr(request.state, "user", None)
        return {
            "roots": roots(scope),
            "restricted": scope is not None,
            # Restricted by the account (an administrator's choice), not the server
            "per_user": bool(user and user.get("role") != "admin"),
            "separator": os.sep,
            "recent": recent(ctx, scope),
        }

    @app.get("/fs/browse", tags=["folders"], dependencies=secured)
    def fs_browse(request: Request, path: str = Query(..., description="Folder to list")):
        """Sub-folders of a server folder with the images and PDFs directly in each."""
        try:
            return browse(folder_scope(settings, request), path)
        except FolderError as error:
            fail(error)

    @app.get("/fs/check", tags=["folders"], dependencies=secured)
    def fs_check(
        request: Request,
        path: str = Query(..., description="Folder to check"),
        recursive: bool = Query(True, description="Count subfolders too"),
    ):
        """ok, images and pdfs for a pasted path, or a plain error message."""
        return check(folder_scope(settings, request), path, recursive)

    @app.get("/fs/recent", tags=["folders"], dependencies=secured)
    def fs_recent(request: Request, check_exists: Optional[bool] = Query(False)):
        folders = recent(ctx, folder_scope(settings, request))
        if check_exists:
            return {
                "folders": [
                    {"path": f, "exists": Path(f).is_dir()} for f in folders
                ]
            }
        return {"folders": folders}
