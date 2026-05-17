"""Tests for the bot worker's pure helpers.

Most of `bot/worker.py` is wired into the python-telegram-bot async
loop and not unit-testable without a live Telegram. `_clean_name` is
the one piece of pure logic that decides how a forwarded archive ends
up labelled on Drive — freezing its rules here protects the folder
naming the scanner walker later relies on.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_worker():
    """Stub heavy deps (telegram + google + bot's own drive_writer)
    before loading worker.py so the test only ever exercises the
    local helpers — no need to actually have python-telegram-bot
    installed in the test environment."""
    if "bot.worker" in sys.modules:
        return sys.modules["bot.worker"]

    # Stub `bot.drive_writer` — worker imports two symbols at module
    # load time, neither of which we exercise in unit tests.
    fake_dw = types.ModuleType("drive_writer")
    fake_dw.folder_exists_nonempty = lambda *a, **kw: None
    fake_dw.upload_dir_tree = lambda *a, **kw: ""
    fake_dw.upload_model_files = lambda *a, **kw: ""
    sys.modules["drive_writer"] = fake_dw

    # Stub the python-telegram-bot namespace. The worker only uses
    # these symbols for type annotations + the application builder
    # (which never gets exercised in tests).
    fake_telegram = types.ModuleType("telegram")
    fake_telegram.Message = type("Message", (), {})
    fake_telegram.Update = type("Update", (), {})
    sys.modules["telegram"] = fake_telegram

    fake_telegram_ext = types.ModuleType("telegram.ext")
    fake_telegram_ext.Application = type("Application", (), {})
    fake_telegram_ext.ApplicationBuilder = type("ApplicationBuilder", (), {})
    fake_telegram_ext.ContextTypes = type("ContextTypes", (), {"DEFAULT_TYPE": object})
    fake_telegram_ext.MessageHandler = type("MessageHandler", (), {})
    fake_telegram_ext.filters = types.SimpleNamespace(ALL=None)
    sys.modules["telegram.ext"] = fake_telegram_ext

    fake_telegram_request = types.ModuleType("telegram.request")
    fake_telegram_request.HTTPXRequest = type("HTTPXRequest", (), {})
    sys.modules["telegram.request"] = fake_telegram_request

    # Make `bot/` importable as a package root.
    pkg = types.ModuleType("bot")
    pkg.__path__ = [str(ROOT / "bot")]
    sys.modules.setdefault("bot", pkg)

    spec = importlib.util.spec_from_file_location(
        "bot.worker", ROOT / "bot" / "worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["bot.worker"] = module
    spec.loader.exec_module(module)
    return module


worker = _load_worker()


@pytest.mark.parametrize("filename,expected_contains", [
    ("Bastet_Figures_..._MOXOMOR.rar", "MOXOMOR"),
    ("Mithril Helmet @Print3DWorld.zip", "Mithril Helmet"),
    ("Geralt_STL.7z", "Geralt"),
    ("Triss.stl", "Triss"),
    # @handle suffix stripped even when filename has spaces already
    ("Cool Model @SomeHandle.rar", "Cool Model"),
    # Multiple underscores collapse into separator
    ("Author__Model__Variant.rar", "Author"),
])
def test_clean_name_strips_extensions_handles_and_bridges(filename, expected_contains):
    out = worker._clean_name(filename)
    assert expected_contains in out
    # extension never bleeds through
    assert not out.endswith((".rar", ".zip", ".7z", ".stl"))


def test_clean_name_falls_back_to_filename_on_pathological_input():
    # `a.rar` would clean to empty; helper guards against that and
    # returns the original.
    assert worker._clean_name("a.rar")


def test_allowed_user_ids_parses_csv(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "111, 222 ,333")
    assert worker._allowed_user_ids() == {111, 222, 333}


def test_allowed_user_ids_empty_returns_empty_set(monkeypatch):
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    assert worker._allowed_user_ids() == set()


def test_allowed_user_ids_ignores_non_numeric(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "111,abc,222")
    assert worker._allowed_user_ids() == {111, 222}


# --- _extract / _unwrap_single_dir ----------------------------------------

def test_extract_zip(tmp_path):
    import zipfile as zf
    archive = tmp_path / "model.zip"
    with zf.ZipFile(archive, "w") as z:
        z.writestr("body.stl", "solid body\nendsolid")
        z.writestr("head.stl", "solid head\nendsolid")
    dest = tmp_path / "out"
    worker._extract(archive, dest)
    assert (dest / "body.stl").exists()
    assert (dest / "head.stl").exists()


def test_extract_zip_preserves_subfolders(tmp_path):
    import zipfile as zf
    archive = tmp_path / "model.zip"
    with zf.ZipFile(archive, "w") as z:
        z.writestr("Presupported/body.stl", "data")
        z.writestr("Unsupported/body.stl", "data")
    dest = tmp_path / "out"
    worker._extract(archive, dest)
    assert (dest / "Presupported" / "body.stl").exists()
    assert (dest / "Unsupported" / "body.stl").exists()


def test_extract_unsupported_ext_raises(tmp_path):
    import pytest
    fake = tmp_path / "model.xyz"
    fake.write_bytes(b"data")
    with pytest.raises((ValueError, Exception)):
        worker._extract(fake, tmp_path / "out")


def test_unwrap_single_dir_unwraps(tmp_path):
    inner = tmp_path / "Model Name"
    inner.mkdir()
    (inner / "body.stl").write_text("solid")
    assert worker._unwrap_single_dir(tmp_path) == inner


def test_unwrap_single_dir_keeps_flat(tmp_path):
    (tmp_path / "body.stl").write_text("solid")
    (tmp_path / "head.stl").write_text("solid")
    assert worker._unwrap_single_dir(tmp_path) == tmp_path


def test_unwrap_single_dir_keeps_mixed(tmp_path):
    (tmp_path / "body.stl").write_text("solid")
    sub = tmp_path / "Presupported"
    sub.mkdir()
    assert worker._unwrap_single_dir(tmp_path) == tmp_path
