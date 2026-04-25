"""Tests for the Drive walker — generic-folder collapse, model
classification, image distribution rules, and same-display-name merge.

The walker is exercised end-to-end against synthetic Drive trees so
the regressions we already fixed (Inuyasha sub-folder fragmentation,
Kratos beauty-shot distribution, "April 2026" promo cross-pollution,
.7z archives as model files, render-only sub-folders) stay fixed.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# --- Fake Drive ------------------------------------------------------


class _FakeFile:
    """Minimal stand-in for scanner.drive.DriveFile."""

    def __init__(
        self,
        id: str,
        name: str,
        *,
        is_folder: bool = False,
        image: bool = False,
        stl: bool = False,
        size: int = 1_000_000,
    ):
        self.id = id
        self.name = name
        self._is_folder = is_folder
        if is_folder:
            self.mime_type = "application/vnd.google-apps.folder"
        elif image:
            self.mime_type = "image/jpeg"
        elif stl:
            self.mime_type = "model/stl"
        else:
            self.mime_type = "application/octet-stream"
        self.size = size
        self.width = 800
        self.height = 600
        self.web_view_link = f"https://drive.google.com/file/d/{id}/view"
        self.modified_time = None
        self.shortcut_target_id = None
        self.shortcut_target_mime = None
        self.thumbnail_link = None

    @property
    def is_folder(self):
        return self._is_folder

    @property
    def effective_id(self):
        return self.id

    @property
    def effective_mime(self):
        return self.mime_type


class _FakeDriveClient:
    def __init__(self, tree: dict[str, list[_FakeFile]]):
        self.tree = tree

    def list_children(self, folder_id: str):
        return iter(self.tree.get(folder_id, []))


def _load_walker():
    """Load scanner.walker without google deps in scope."""
    if "scanner.walker" in sys.modules:
        # may have been replaced by test_selector with a stub — drop it.
        mod = sys.modules["scanner.walker"]
        if not hasattr(mod, "walk"):
            del sys.modules["scanner.walker"]

    # Real walker imports from .drive — provide a minimal stub if needed.
    fake_drive = types.ModuleType("scanner.drive")

    class _StubDriveClient:
        pass

    fake_drive.DriveClient = _StubDriveClient
    fake_drive.DriveFile = _FakeFile
    sys.modules["scanner.drive"] = fake_drive

    pkg = types.ModuleType("scanner")
    pkg.__path__ = [str(ROOT / "scanner")]
    sys.modules.setdefault("scanner", pkg)

    if "scanner.walker" in sys.modules and not hasattr(sys.modules["scanner.walker"], "walk"):
        del sys.modules["scanner.walker"]
    if "scanner.walker" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "scanner.walker", ROOT / "scanner" / "walker.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["scanner.walker"] = module
        spec.loader.exec_module(module)
    return sys.modules["scanner.walker"]


walker = _load_walker()


# --- _is_generic_name / _meaningful_name ----------------------------


@pytest.mark.parametrize("name,expected", [
    # Generic — every token is generic
    ("STL", True),
    ("Bust", True),
    ("75mm", True),
    ("178mm_split", True),
    ("1/10 Scale", True),
    ("1/10 Scale Split", True),
    ("75mm Miniature", True),
    ("Presupports", True),
    ("Presupport", True),
    # Non-generic — at least one meaningful token
    ("AhsokaTano", False),
    ("Captain America", False),
    ("Eleven - Stranger Things", False),
    ("TifaBust", False),
    ("Asuka_STL", False),
])
def test_is_generic_name(name, expected):
    assert walker._is_generic_name(name) is expected


@pytest.mark.parametrize("path,leaf,expected", [
    # Walker climbs to non-generic ancestor when leaf is generic
    (["April 2026", "SailorMoon"], "STL", "SailorMoon"),
    (["March 2026", "Goku"], "75mm", "Goku"),
    (["April 2026", "Naruto"], "1/10 Scale", "Naruto"),
    (["April 2026", "Sanji"], "Bust", "Sanji"),
    # Trailing format suffix stripped
    (["April 2026", "Tifa"], "TifaBust_STL", "TifaBust"),
    (["April 2026", "Asuka Langley"], "Asuka_STL", "Asuka"),
    # Already-good names left alone
    (["April 2026"], "AhsokaTano", "AhsokaTano"),
    (["April 2026"], "Captain America", "Captain America"),
])
def test_meaningful_name_climbs_to_non_generic(path, leaf, expected):
    assert walker._meaningful_name(path, leaf) == expected


# --- walk(): full-tree integration -----------------------------------


def _walk(tree: dict, root_id: str = "root"):
    return walker.walk(_FakeDriveClient(tree), root_id)


def test_flat_model_with_image_and_stl():
    tree = {
        "root": [_FakeFile("ahsoka", "AhsokaTano", is_folder=True)],
        "ahsoka": [
            _FakeFile("ahsoka_jpg", "Poster.jpg", image=True),
            _FakeFile("ahsoka_stl", "model.stl", stl=True),
        ],
    }
    models = _walk(tree)
    assert len(models) == 1
    m = models[0]
    assert m.display_name == "AhsokaTano"
    assert m.release is None
    assert len(m.image_candidates) == 1
    assert len(m.stl_candidates) == 1


def test_inuyasha_collapses_generic_subfolders():
    """Inuyasha/STL/Bust + Inuyasha/STL/1-10 Scale + Inuyasha/Presupports/Bust/STL
    should collapse into ONE model named Inuyasha with all STLs aggregated."""
    tree = {
        "root": [_FakeFile("apr", "April 2026 Release", is_folder=True)],
        "apr": [_FakeFile("inu", "Inuyasha", is_folder=True)],
        "inu": [
            _FakeFile("inu_stl", "STL", is_folder=True),
            _FakeFile("inu_pre", "Presupports", is_folder=True),
            _FakeFile("inu_render", "render.jpg", image=True),
        ],
        "inu_stl": [
            _FakeFile("inu_bust", "Bust", is_folder=True),
            _FakeFile("inu_scale", "1/10 Scale", is_folder=True),
        ],
        "inu_bust": [_FakeFile("ib_file", "inuyasha_bust.stl", stl=True)],
        "inu_scale": [_FakeFile("is_file", "inuyasha_body.stl", stl=True)],
        "inu_pre": [_FakeFile("ip", "Bust", is_folder=True)],
        "ip": [_FakeFile("ip_stl", "STL", is_folder=True)],
        "ip_stl": [_FakeFile("ip_file", "inuyasha_pre.stl", stl=True)],
    }
    models = _walk(tree)
    assert len(models) == 1
    m = models[0]
    assert m.display_name == "Inuyasha"
    assert m.release == "April 2026 Release"
    assert len(m.stl_candidates) == 3
    assert len(m.image_candidates) == 1


def test_kratos_same_display_name_distributes_group_images():
    """Kratos_STL and Kratos_Presupport both display as 'Kratos' — the
    parent folder's BeautyShot must be passed down to both before merge."""
    tree = {
        "root": [_FakeFile("kg", "Kratos from God of War", is_folder=True)],
        "kg": [
            _FakeFile("kg_stl", "Kratos_STL", is_folder=True),
            _FakeFile("kg_pre", "Kratos_Presupport", is_folder=True),
            _FakeFile("kg_beauty", "Kratos_BeautyShot.jpg", image=True),
        ],
        "kg_stl": [_FakeFile("kg_stl_inner", "STL", is_folder=True)],
        "kg_stl_inner": [_FakeFile("kg_stl_file", "kratos_a.stl", stl=True)],
        "kg_pre": [_FakeFile("kg_pre_inner", "STL", is_folder=True)],
        "kg_pre_inner": [_FakeFile("kg_pre_file", "kratos_b.stl", stl=True)],
    }
    models = _walk(tree)
    # Two pending models with same display_name "Kratos"
    assert {m.display_name for m in models} == {"Kratos"}
    assert len(models) == 2
    for m in models:
        names = [f.name for f in m.image_candidates]
        assert "Kratos_BeautyShot.jpg" in names


def test_release_promo_not_smeared_to_distinct_children():
    """A multi-character release with a promo at the top level must NOT
    pass that image down to every child (regression: Edward poster ended
    up on every April 2026 card)."""
    tree = {
        "root": [_FakeFile("apr", "April 2026 Lootbox Release", is_folder=True)],
        "apr": [
            _FakeFile("promo", "edward_promo.jpg", image=True),
            _FakeFile("eleven", "Eleven", is_folder=True),
            _FakeFile("mike", "Mike", is_folder=True),
        ],
        "eleven": [_FakeFile("e_stl", "STL", is_folder=True)],
        "e_stl": [_FakeFile("e_file", "eleven.stl", stl=True)],
        "mike": [_FakeFile("m_stl", "STL", is_folder=True)],
        "m_stl": [_FakeFile("m_file", "mike.stl", stl=True)],
    }
    models = _walk(tree)
    assert {m.display_name for m in models} == {"Eleven", "Mike"}
    for m in models:
        assert m.image_candidates == []  # promo not distributed


def test_render_subfolder_images_bubble_up():
    """A `render images/` sibling folder containing only images (no STLs)
    must still have its content collected by the model folder above."""
    tree = {
        "root": [_FakeFile("apr", "April 2026", is_folder=True)],
        "apr": [_FakeFile("will", "Will - Stranger Things", is_folder=True)],
        "will": [
            _FakeFile("will_stl", "STL", is_folder=True),
            _FakeFile("will_renders", "render images", is_folder=True),
        ],
        "will_stl": [_FakeFile("will_file", "will.stl", stl=True)],
        "will_renders": [
            _FakeFile("beauty", "Beauty shot.jpg", image=True),
            _FakeFile("pose", "Action_pose.jpg", image=True),
        ],
    }
    models = _walk(tree)
    assert len(models) == 1
    m = models[0]
    assert m.display_name == "Will - Stranger Things"
    names = sorted(f.name for f in m.image_candidates)
    assert names == ["Action_pose.jpg", "Beauty shot.jpg"]


def test_archive_files_are_treated_as_stls():
    """`Geralt_STL.7z` (and friends) should make the folder count as a
    model even when no loose .stl files are present."""
    tree = {
        "root": [_FakeFile("geralt", "Geralt", is_folder=True)],
        "geralt": [
            _FakeFile("g_archive", "Geralt_STL.7z"),
            _FakeFile("g_cover", "Geralt.jpg", image=True),
        ],
    }
    models = _walk(tree)
    assert len(models) == 1
    m = models[0]
    assert m.display_name == "Geralt"
    assert [s.file.name for s in m.stl_candidates] == ["Geralt_STL.7z"]


def test_pre_sliced_resin_files_are_treated_as_stls():
    """`.ctb` (ChituBox) and `.goo` (Elegoo native) are pre-sliced resin
    formats — surface them so users can grab the printer-ready file
    instead of re-slicing the STL."""
    tree = {
        "root": [_FakeFile("geralt", "Geralt", is_folder=True)],
        "geralt": [
            _FakeFile("g_ctb", "Geralt_S4U.ctb"),
            _FakeFile("g_goo", "Geralt.goo"),
            _FakeFile("g_cover", "Geralt.jpg", image=True),
        ],
    }
    models = _walk(tree)
    assert len(models) == 1
    names = sorted(s.file.name for s in models[0].stl_candidates)
    assert names == ["Geralt.goo", "Geralt_S4U.ctb"]


def test_stl_entry_parent_chain_includes_all_ancestors():
    """parent_chain on each StlEntry must contain every folder name from
    the root down to the immediate parent — used by the Saturn detector
    to spot markers that sit several levels above the file."""
    tree = {
        "root": [_FakeFile("apr", "April 2026 Release", is_folder=True)],
        "apr": [_FakeFile("s4u", "Saturn 4 Ultra", is_folder=True)],
        "s4u": [_FakeFile("geralt", "Geralt", is_folder=True)],
        "geralt": [_FakeFile("pre", "Presupports", is_folder=True)],
        "pre": [_FakeFile("stl", "STL", is_folder=True)],
        "stl": [_FakeFile("g_file", "geralt.stl", stl=True)],
    }
    models = _walk(tree)
    assert len(models) == 1
    m = models[0]
    assert len(m.stl_candidates) == 1
    chain = m.stl_candidates[0].parent_chain
    # Chain should include every ancestor folder back to (but excluding)
    # the Drive root, with the immediate parent (`STL`) at the end.
    assert "Saturn 4 Ultra" in chain
    assert chain[-1] == "STL"


def test_folder_with_neither_stl_nor_archive_skipped():
    tree = {
        "root": [_FakeFile("lore", "Lore", is_folder=True)],
        "lore": [_FakeFile("text", "story.txt")],
    }
    assert _walk(tree) == []
