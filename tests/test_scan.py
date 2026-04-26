"""Tests for scan.py's incremental-mode helpers.

The helpers don't need DriveClient or the walker — they're pure
filesystem operations on a previously-written manifest. Testing them
directly avoids dragging the google-api-python-client stack into the
test environment.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_scan():
    """Import scanner.scan with only Drive stubbed.

    Loading the real selector / thumbs / walker on top of a stubbed
    drive keeps sys.modules in a state the other test modules can also
    use — stubbing those would cause test_selector.py / test_walker.py
    to pick up our stubs and fail when run in the same session.
    """
    if "scanner.scan" in sys.modules:
        return sys.modules["scanner.scan"]

    fake_drive = types.ModuleType("scanner.drive")
    class DriveClient: pass
    class DriveFile: pass
    fake_drive.DriveClient = DriveClient
    fake_drive.DriveFile = DriveFile
    sys.modules["scanner.drive"] = fake_drive

    pkg = types.ModuleType("scanner")
    pkg.__path__ = [str(ROOT / "scanner")]
    sys.modules.setdefault("scanner", pkg)

    def _load_real(name: str):
        if name in sys.modules and hasattr(sys.modules[name], "__file__"):
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "scanner" / f"{name.split('.')[-1]}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    _load_real("scanner.walker")
    _load_real("scanner.selector")
    _load_real("scanner.thumbs")

    spec = importlib.util.spec_from_file_location(
        "scanner.scan", ROOT / "scanner" / "scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scanner.scan"] = module
    spec.loader.exec_module(module)
    return module


scan = _load_scan()


# --- _load_existing_manifest -----------------------------------------


def test_load_existing_manifest_missing_file(tmp_path):
    assert scan._load_existing_manifest(tmp_path / "nope.json") == {}


def test_load_existing_manifest_returns_id_keyed_map(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "generated_at": "2026-04-26T00:00:00Z",
        "releases": ["April 2026"],
        "models": [
            {"id": "abc", "name": "Geralt", "release": "April 2026"},
            {"id": "def", "name": "Triss",  "release": "April 2026"},
        ],
    }))
    out = scan._load_existing_manifest(p)
    assert set(out) == {"abc", "def"}
    assert out["abc"]["name"] == "Geralt"
    assert out["def"]["name"] == "Triss"


def test_load_existing_manifest_skips_entries_without_id(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "models": [
            {"id": "abc", "name": "Geralt"},
            {"name": "MissingId"},  # malformed entry — silently dropped
            {"id": None, "name": "NullId"},  # explicit null id — also dropped
        ],
    }))
    out = scan._load_existing_manifest(p)
    assert set(out) == {"abc"}


def test_load_existing_manifest_unparseable_returns_empty(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text("{not valid json")
    # Should NOT raise — caller falls back to a full scan
    assert scan._load_existing_manifest(p) == {}


# --- _prune_orphan_thumbs --------------------------------------------


def test_prune_orphan_thumbs_keeps_referenced_files(tmp_path):
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    (thumbs / "kept_a.jpg").write_bytes(b"a")
    (thumbs / "kept_b.jpg").write_bytes(b"b")
    (thumbs / "orphan.jpg").write_bytes(b"o")

    manifest = [
        {"id": "1", "thumb": "thumbs/kept_a.jpg"},
        {"id": "2", "thumb": "thumbs/kept_b.jpg"},
    ]
    removed = scan._prune_orphan_thumbs(thumbs, manifest)
    assert removed == 1
    remaining = sorted(p.name for p in thumbs.iterdir())
    assert remaining == ["kept_a.jpg", "kept_b.jpg"]


def test_prune_orphan_thumbs_handles_null_thumb(tmp_path):
    """Models without a cover (manifest['thumb'] is None) must not crash
    the prune pass — they just contribute nothing to the referenced set."""
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    (thumbs / "still_used.jpg").write_bytes(b"x")
    (thumbs / "orphan.jpg").write_bytes(b"o")

    manifest = [
        {"id": "1", "thumb": "thumbs/still_used.jpg"},
        {"id": "2", "thumb": None},
    ]
    removed = scan._prune_orphan_thumbs(thumbs, manifest)
    assert removed == 1
    assert (thumbs / "still_used.jpg").exists()
    assert not (thumbs / "orphan.jpg").exists()


def test_prune_orphan_thumbs_no_thumbs_dir(tmp_path):
    # If thumbs_dir doesn't exist, return 0 cleanly.
    assert scan._prune_orphan_thumbs(tmp_path / "nope", []) == 0
