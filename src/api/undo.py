"""
Undo and redo for one sheet.

Every saved change of a sheet's result.json (a correction, an accept, a
verification, a regrade, a hand alignment, typed values) first copies the
stored result into <scan dir>/undo/, with what was done and by whom. Undo
puts the previous copy back and keeps the current one for redo; a new change
clears the redo list. Zone crops, which a regrade or hand alignment rewrite,
are kept with the copy; the aligned image is redrawn from the stored geometry.
"""

import json
import shutil
import time
import uuid

from src.api.storage import read_json, write_json_atomic

MAX_STEPS = 20
# Actions that rewrite the zone crops (and the aligned image)
IMAGE_ACTIONS = ("regrade", "hand alignment")


class UndoStack:
    def __init__(self, scan_dir):
        self.dir = scan_dir / "undo"
        self.index_file = self.dir / "stack.json"

    def read(self):
        stack = read_json(self.index_file, None) or {}
        return {"undo": list(stack.get("undo") or []), "redo": list(stack.get("redo") or [])}

    def labels(self):
        stack = self.read()
        last = lambda steps: steps[-1] if steps else None  # noqa: E731
        return {"undo": last(stack["undo"]), "redo": last(stack["redo"])}

    def _write(self, stack):
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.index_file, stack)

    def _keep(self, result, action, user):
        """Copy a result (and its zone crops for image actions) as a step."""
        step = {
            "id": uuid.uuid4().hex[:12],
            "action": action,
            "by": user,
            "at": time.time(),
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.dir / f"{step['id']}.json", result)
        zones = self.dir.parent / "zones"
        if action in IMAGE_ACTIONS and zones.is_dir():
            shutil.copytree(zones, self.dir / f"{step['id']}.zones")
            step["zones"] = True
        return step

    def _drop(self, step):
        (self.dir / f"{step['id']}.json").unlink(missing_ok=True)
        shutil.rmtree(self.dir / f"{step['id']}.zones", ignore_errors=True)

    def push(self, previous, action, user):
        """previous: the stored result before this change."""
        stack = self.read()
        stack["undo"].append(self._keep(previous, action, user))
        for step in stack["redo"]:
            self._drop(step)
        stack["redo"] = []
        while len(stack["undo"]) > MAX_STEPS:
            self._drop(stack["undo"].pop(0))
        self._write(stack)

    def move(self, current, user, direction):
        """Undo (direction "undo") or redo one step. Returns (result to store,
        the step taken) or (None, None) when there is nothing to do."""
        other = "redo" if direction == "undo" else "undo"
        stack = self.read()
        if not stack[direction]:
            return None, None
        step = stack[direction].pop()
        restored = read_json(self.dir / f"{step['id']}.json", None)
        if restored is None:
            self._write(stack)
            return None, None
        # The current state becomes the step going the other way, under the
        # same action name (undoing "regrade" can be redone as "regrade")
        back = self._keep(current, step["action"], user)
        zones = self.dir.parent / "zones"
        saved_zones = self.dir / f"{step['id']}.zones"
        if step.get("zones") or back.get("zones"):
            shutil.rmtree(zones, ignore_errors=True)
            if saved_zones.is_dir():
                shutil.copytree(saved_zones, zones)
        self._drop(step)
        stack[other].append(back)
        self._write(stack)
        return restored, step


def same_image(a, b):
    """Whether two results draw the same aligned image."""
    keys = ("geometry", "regrade", "manual_alignment", "template_version")
    return json.dumps([a.get(k) for k in keys], sort_keys=True, default=str) == json.dumps(
        [b.get(k) for k in keys], sort_keys=True, default=str
    )
