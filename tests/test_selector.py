"""Pure-function tests for cover-selection logic.

These freeze the current behaviour: the regex tiers, the hint pool,
the series-number ordering, and the stopword filter. Any future tweak
to selector.py needs to update these expectations on purpose, not by
accident.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_selector():
    """Import scanner.selector without pulling in google-api-python-client
    (which transitively imports cryptography and would otherwise be a
    test-time dependency we don't need)."""
    if "scanner.selector" in sys.modules:
        return sys.modules["scanner.selector"]

    fake_drive = types.ModuleType("scanner.drive")
    class DriveFile: pass
    fake_drive.DriveClient = object
    fake_drive.DriveFile = DriveFile
    sys.modules["scanner.drive"] = fake_drive

    fake_walker = types.ModuleType("scanner.walker")
    class Model: pass
    class StlEntry: pass
    fake_walker.Model = Model
    fake_walker.StlEntry = StlEntry
    sys.modules["scanner.walker"] = fake_walker

    pkg = types.ModuleType("scanner")
    pkg.__path__ = [str(ROOT / "scanner")]
    sys.modules.setdefault("scanner", pkg)

    spec = importlib.util.spec_from_file_location(
        "scanner.selector", ROOT / "scanner" / "selector.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scanner.selector"] = module
    spec.loader.exec_module(module)
    return module


selector = _load_selector()


# --- _is_hard_pick (primary tier) -----------------------------------


@pytest.mark.parametrize("filename,model_name,expected", [
    # Beauty / BeautyShot / BeautyPic
    ("Beauty shot.jpg", "Anything", True),
    ("BeautyShot_01.png", "Anything", True),
    ("BeautyPic.jpg", "Anything", True),
    ("Beauty_Shot.jpg", "Anything", True),
    ("beauty-shot.jpeg", "Anything", True),
    # BS NN abbreviation
    ("BellBeast BS 01.jpg", "Chibi Bell Beast - Hollow Knight Silksong", True),
    ("BS_001.jpg", "Whatever", True),
    ("BS-01.jpg", "Whatever", True),
    ("BS01.jpg", "Whatever", True),
    # FinalRender
    ("FinalRender.jpg", "Zelda", True),
    ("Final_Render.jpg", "Zelda", True),
    ("Final-Render.jpg", "Zelda", True),
    # Bare Final
    ("Final.jpg", "Anything", True),
    # FolderName.jpg
    ("Geralt.jpg", "Geralt from God of War", True),
    ("Geralt.jpg", "Geralt", True),
    ("Asuka.jpg", "Asuka_STL", True),
    # NEGATIVES — letter before BS / Cover etc.
    ("ABS_engine.jpg", "X", False),
    ("TurntableBS.jpg", "X", False),
    # cover / poster moved to secondary
    ("cover.jpg", "X", False),
    ("Poster_01.jpg", "X", False),
    # Triss.jpg in unrelated folder is *not* a hard pick (no token match)
    ("Triss.jpg", "Wiedzmin Chibi", False),
    # generic stopwords don't get promoted
    ("Parts.jpg", "Inuyasha", False),
    ("Render.jpg", "X", False),
    ("Turntable_01.jpg", "X", False),
    # filenames with extra tokens beyond the proper noun lose hard-pick
    ("Inuyasha_render.jpg", "Inuyasha", False),
    # plain numbered renders
    ("12.jpg", "Geralt", False),
    ("IMG_001.jpg", "X", False),
])
def test_is_hard_pick(filename, model_name, expected):
    assert selector._is_hard_pick(filename, model_name) is expected


# --- _is_secondary_pick ----------------------------------------------


@pytest.mark.parametrize("filename,expected", [
    # cover variants
    ("cover.jpg", True),
    ("Cover.jpg", True),
    ("Foo_Cover.jpg", True),
    ("Cover_01.jpg", True),
    # poster variants
    ("Poster.jpg", True),
    ("Poster_01.jpg", True),
    ("Poster 01.jpg", True),
    ("Foo_Poster.jpg", True),
    ("Foo_Poster_01.jpg", True),
    # negatives — letter before
    ("BookCover.jpg", False),
    ("WallPoster.jpg", False),
    ("LamPoster.jpg", False),
    # primary tier files are not secondary
    ("Beauty shot.jpg", False),
    ("Final.jpg", False),
    ("FinalRender.jpg", False),
    # random
    ("Random.jpg", False),
    ("12.jpg", False),
])
def test_is_secondary_pick(filename, expected):
    assert selector._is_secondary_pick(filename) is expected


# --- _has_hint (soft hint pool) --------------------------------------


@pytest.mark.parametrize("filename,model_name,expected", [
    # final / render anywhere as a word
    ("Final.jpg", "Geralt", True),
    ("FinalRender.jpg", "Zelda", True),
    ("Render.jpg", "X", True),
    ("MainRender.jpg", "X", True),
    ("TurntableFinal.jpg", "Zelda", True),
    # proper noun (Triss-in-Wiedzmin case)
    ("Triss.jpg", "Wiedzmin Chibi", True),
    ("Geralt.jpg", "Other folder", True),
    # folder-name token match
    ("Asuka_render.jpg", "Asuka_STL", True),
    ("Inuyasha_back.jpg", "Inuyasha", True),
    # negatives
    ("12.jpg", "Geralt", False),
    ("IMG_001.jpg", "X", False),
    ("Parts.jpg", "Inuyasha", False),  # parts is stopword on both sides
])
def test_has_hint(filename, model_name, expected):
    assert selector._has_hint(filename, model_name) is expected


# --- _series_number (ordering tiebreaker) ----------------------------


@pytest.mark.parametrize("filename,expected", [
    ("BellBeast BS 01.jpg", 1),
    ("BellBeast BS 02.jpg", 2),
    ("Queen-of-Blades---Starcraft-1_edited.jpg", 1),
    ("Queen-of-Blades---Starcraft-7_edited.jpg", 7),
    ("Asuka_v2_edited.jpg", 2),
    ("Poster_001.jpg", 1),
    ("BeautyShot.jpg", 999_999),
    ("Render.jpg", 999_999),
])
def test_series_number(filename, expected):
    assert selector._series_number(filename) == expected


# --- _hints_for (complete classification) ----------------------------


def test_hints_for_returns_all_matching_labels():
    # FinalRender hits both FINAL and RENDER patterns
    hits = selector._hints_for("FinalRender.jpg", "Zelda")
    assert "final" in hits and "render" in hits


def test_hints_for_proper_noun_in_folder():
    hits = selector._hints_for("Geralt.jpg", "Geralt from God of War")
    assert "propnoun" in hits
    assert "foldername" in hits


def test_hints_for_unhinted_file_is_empty():
    assert selector._hints_for("12.jpg", "Geralt") == []


def test_hints_for_beauty_label():
    assert "beauty" in selector._hints_for("Beauty shot.jpg", "X")


# --- _name_tokens (folder-name stopword filtering) -------------------


def test_name_tokens_filters_stopwords():
    # "render" / "stl" / "model" / etc. should be filtered out
    tokens = selector._name_tokens("Inuyasha STL render images")
    assert tokens == {"inuyasha"}


def test_name_tokens_keeps_short_proper_nouns_above_threshold():
    tokens = selector._name_tokens("Eiko - Final Fantasy IX")
    assert "eiko" in tokens
    assert "fantasy" in tokens
    assert "ix" not in tokens  # 2 chars, below threshold


# --- Beauty/BS hard-pick ordering rule -------------------------------


def test_bs_lower_number_beats_higher_number():
    """BS 01 should outrank BS 02 even when BS 02 is a larger file."""
    assert selector._is_hard_pick("BellBeast BS 01.jpg", "Bell Beast")
    assert selector._is_hard_pick("BellBeast BS 02.jpg", "Bell Beast")
    assert selector._series_number("BellBeast BS 01.jpg") < selector._series_number(
        "BellBeast BS 02.jpg"
    )
