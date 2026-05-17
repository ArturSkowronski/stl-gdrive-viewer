"""Tests for the Telegram channel scraper.

Parser is exercised against an inline HTML snapshot that mirrors the
shape of `t.me/s/<channel>` for a media-group + document pair (one
album of two photos followed by a .rar file in the same minute, plus
a standalone Mithril Helmet image+zip pair). This keeps the test
network-free and stable when Telegram tweaks unrelated parts of the
widget markup.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_telegram():
    if "scanner.telegram" in sys.modules:
        return sys.modules["scanner.telegram"]
    pkg = types.ModuleType("scanner")
    pkg.__path__ = [str(ROOT / "scanner")]
    sys.modules.setdefault("scanner", pkg)
    spec = importlib.util.spec_from_file_location(
        "scanner.telegram", ROOT / "scanner" / "telegram.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scanner.telegram"] = module
    spec.loader.exec_module(module)
    return module


telegram = _load_telegram()


# Two media-group posts back-to-back: an image+image+document combo
# (Bastet RPD pack) and a single-image + document combo (Mithril Helmet).
SAMPLE_HTML = """
<section class="tgme_channel_history">
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message" data-post="Best_STL_3D/100">
      <a class="tgme_widget_message_photo_wrap"
         style="background-image:url('https://cdn4.cdn-telegram.org/file/aaa.jpg')"></a>
      <a class="tgme_widget_message_photo_wrap"
         style="background-image:url('https://cdn4.cdn-telegram.org/file/bbb.jpg')"></a>
      <div class="tgme_widget_message_text">Bastet Figures – RPD</div>
    </div>
  </div>
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message" data-post="Best_STL_3D/101">
      <a class="tgme_widget_message_document" href="https://t.me/Best_STL_3D/101">
        <div class="tgme_widget_message_document_title">Bastet_Figures_..._MOXOMOR.rar</div>
        <div class="tgme_widget_message_document_extra">1.4 GB</div>
      </a>
    </div>
  </div>
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message" data-post="Best_STL_3D/200">
      <a class="tgme_widget_message_photo_wrap"
         style="background-image:url('https://cdn4.cdn-telegram.org/file/helmet.jpg')"></a>
    </div>
  </div>
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message" data-post="Best_STL_3D/201">
      <a class="tgme_widget_message_document" href="https://t.me/Best_STL_3D/201">
        <div class="tgme_widget_message_document_title">Mithril Helmet @Print3DWorld.zip</div>
        <div class="tgme_widget_message_document_extra">160.4 MB</div>
      </a>
    </div>
  </div>
  <div class="tgme_widget_message service_message" data-post="Best_STL_3D/99">
    <div>Channel created</div>
  </div>
</section>
"""


def test_parse_page_groups_album_and_document():
    models = telegram.parse_page(SAMPLE_HTML, "Best_STL_3D")
    # Two models — one for each document. Photo-only messages aren't
    # surfaced on their own (they're cover candidates for the next doc).
    assert len(models) == 2
    moxomor, helmet = models
    assert moxomor.message_id == 101
    assert moxomor.cover_url == "https://cdn4.cdn-telegram.org/file/aaa.jpg"
    assert moxomor.id == "tg:Best_STL_3D:101"
    assert moxomor.message_url == "https://t.me/Best_STL_3D/101"
    assert len(moxomor.files) == 1
    assert moxomor.files[0].name == "Bastet_Figures_..._MOXOMOR.rar"
    assert moxomor.files[0].size == 1_400_000_000  # 1.4 GB

    assert helmet.message_id == 201
    assert helmet.cover_url == "https://cdn4.cdn-telegram.org/file/helmet.jpg"
    assert helmet.files[0].name == "Mithril Helmet @Print3DWorld.zip"
    assert helmet.files[0].size == 160_400_000


def test_parse_page_skips_service_messages():
    """Channel-creation / pin notices arrive with class `service_message`
    and have no doc — they must not bubble into the model list."""
    models = telegram.parse_page(SAMPLE_HTML, "Best_STL_3D")
    assert all(m.message_id != 99 for m in models)


def test_parse_page_skips_unsupported_file_types():
    html = """
    <div class="tgme_widget_message" data-post="C/1">
      <a class="tgme_widget_message_document">
        <div class="tgme_widget_message_document_title">notes.pdf</div>
        <div class="tgme_widget_message_document_extra">12 MB</div>
      </a>
    </div>
    """
    assert telegram.parse_page(html, "C") == []


def test_parse_page_attaches_doc_to_preceding_photo_only_within_three_ids():
    """Photo at id 200, doc at id 205 — gap of 5 is too wide, cover lost."""
    html = """
    <div class="tgme_widget_message" data-post="C/200">
      <a class="tgme_widget_message_photo_wrap"
         style="background-image:url('https://cdn/photo.jpg')"></a>
    </div>
    <div class="tgme_widget_message" data-post="C/205">
      <a class="tgme_widget_message_document">
        <div class="tgme_widget_message_document_title">model.rar</div>
        <div class="tgme_widget_message_document_extra">1 MB</div>
      </a>
    </div>
    """
    models = telegram.parse_page(html, "C")
    assert len(models) == 1
    assert models[0].cover_url is None


def test_parse_size_handles_si_units():
    assert telegram._parse_size("1.4 GB") == 1_400_000_000
    assert telegram._parse_size("160.4 MB") == 160_400_000
    assert telegram._parse_size("743 KB") == 743_000
    assert telegram._parse_size("12 B") == 12
    assert telegram._parse_size("") is None
    assert telegram._parse_size("unknown") is None
    # European decimal comma (some locales render Telegram sizes that way)
    assert telegram._parse_size("1,4 GB") == 1_400_000_000


def test_clean_name_strips_handles_and_bridges():
    cn = telegram._clean_name
    # @handle suffix and bridge underscores both stripped
    assert "MOXOMOR" in cn("Bastet_Figures_..._MOXOMOR.rar")
    assert "Mithril Helmet" in cn("Mithril Helmet @Print3DWorld.zip")
    # Extension always dropped
    assert ".rar" not in cn("Whatever.rar")
    assert ".zip" not in cn("Whatever.zip")
    # Pathological — never returns empty, falls back to the original
    assert cn("a.rar")  # truthy, doesn't crash


def test_fetch_channel_filters_known_ids():
    """fetch_channel respects the known-id set so callers can skip
    already-indexed messages on every run."""
    # Stub fetch_first_page to return our snapshot
    orig = telegram.fetch_first_page
    telegram.fetch_first_page = lambda channel, opener=None: SAMPLE_HTML
    try:
        fresh = telegram.fetch_channel(
            "Best_STL_3D",
            known_ids={"tg:Best_STL_3D:101"},  # already-seen
        )
        assert [m.message_id for m in fresh] == [201]
    finally:
        telegram.fetch_first_page = orig
