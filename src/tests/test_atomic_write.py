import json
import os
from unittest import mock

import pytest

from src.api import storage


def test_atomic_write_retries_while_windows_holds_the_file(tmp_path):
    """Windows refuses the swap while e.g. an antivirus scan has the old file open."""
    target = tmp_path / "job.json"
    real, calls = os.replace, []

    def held_twice(source, destination):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        return real(source, destination)

    with mock.patch.object(storage.os, "replace", held_twice):
        storage.write_json_atomic(target, {"n": 1})
    assert len(calls) == 3
    assert storage.read_json(target) == {"n": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["job.json"]

    # Still held after about a second: the error comes through, no temp file is left
    with mock.patch.object(storage.os, "replace", side_effect=PermissionError(5, "denied")):
        with pytest.raises(PermissionError):
            storage.write_json_atomic(target, {"n": 2})
    assert storage.read_json(target) == {"n": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["job.json"]


def test_compact_writes_no_spaces(tmp_path):
    target = tmp_path / "result.json"
    storage.write_json_atomic(target, {"a": [1, 2], "b": {"c": 0.5}}, compact=True)
    assert target.read_text() == '{"a":[1,2],"b":{"c":0.5}}'
    assert json.loads(target.read_text()) == {"a": [1, 2], "b": {"c": 0.5}}
