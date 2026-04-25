"""Walks the Drive tree and classifies each folder as model / group / skip.

Rules (post-order DFS):
  - A folder is a MODEL if its subtree contains >=1 STL file AND it doesn't
    contain any sub-folder that is itself a model. I.e. the highest folder
    that "owns" a leaf set of STLs.
  - A folder is a GROUP (release) if it has model descendants. The group's
    name becomes the `release` label for those models (nearest-ancestor wins).
  - Otherwise (no STLs anywhere in subtree) it's skipped.

This handles mixed structures: nested NomNom releases, flat singletons,
and standalone models in the root, all without hardcoding levels.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .drive import DriveClient, DriveFile

log = logging.getLogger(__name__)

IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
STL_EXTS = (".stl",)
MAX_DEPTH = 6

# Tokens that indicate a folder is a generic container, not a model name.
# A folder name made up *entirely* of these (plus digits) is considered
# unusable for display, and the walker climbs up the path to find a real one.
GENERIC_TOKENS = {
    "stl", "stls", "bust", "busts", "split", "splits",
    "presupported", "presupports", "presupport",
    "unsupported", "unsupports", "unsupport",
    "supported", "supports",
    "scale", "miniature", "miniatures", "mini", "minis", "mm",
    "raw", "bonus", "extras", "files", "images", "renders", "lore",
}

# Strips trailing format/variant labels separated by space/_/- so
# "Asuka_STL" -> "Asuka" and "Tifa Bust" -> "Tifa".
_TRAILING_GENERIC_RE = re.compile(
    r"[\s_\-]+(stl|stls|bust|busts|split|splits|"
    r"presupported|presupports|presupport|"
    r"unsupported|unsupports|unsupport|"
    r"supported|supports|"
    r"miniature|miniatures|files|raw)$",
    re.IGNORECASE,
)


def _split_unit(token: str) -> List[str]:
    """`75mm` -> ['75', 'mm']."""
    m = re.match(r"^(\d+)([a-z]+)$", token)
    return [m.group(1), m.group(2)] if m else [token]


def _is_generic_token(tok: str) -> bool:
    if not tok:
        return True
    if tok.isdigit():
        return True
    return tok in GENERIC_TOKENS


def _is_generic_name(name: str) -> bool:
    """True if every word in the name is generic (or just digits/units)."""
    if not name:
        return True
    raw = [t for t in re.split(r"[\s,/_\-]+", name.lower()) if t]
    expanded: List[str] = []
    for t in raw:
        expanded.extend(_split_unit(t))
    return bool(expanded) and all(_is_generic_token(t) for t in expanded)


def _strip_trailing_generic(name: str) -> str:
    cleaned = name.strip()
    while True:
        new = _TRAILING_GENERIC_RE.sub("", cleaned).strip()
        if new == cleaned or not new:
            break
        cleaned = new
    return cleaned or name


def _meaningful_name(folder_path: List[str], leaf_name: str) -> str:
    """Walk up the chain leaf -> root; return the deepest non-generic name,
    with trailing format suffixes (`_STL`, `Bust`, ...) stripped."""
    chain = list(folder_path) + [leaf_name]
    for raw in reversed(chain):
        cleaned = _strip_trailing_generic(raw)
        if cleaned and not _is_generic_name(cleaned):
            return cleaned
    # All names in the chain are generic — best effort: stripped leaf.
    return _strip_trailing_generic(leaf_name) or leaf_name


def _is_stl(f: DriveFile) -> bool:
    return f.effective_mime != "application/vnd.google-apps.folder" and f.name.lower().endswith(
        STL_EXTS
    )


def _is_image(f: DriveFile) -> bool:
    return f.effective_mime in IMAGE_MIMES


@dataclass
class Model:
    name: str  # raw folder name on Drive, never altered
    display_name: str  # cleaned label for UI; equal to `name` when no rename
    folder_id: str
    folder_path: List[str]  # ancestor names, root-relative
    web_view_link: Optional[str]
    release: Optional[str] = None
    image_candidates: List[DriveFile] = field(default_factory=list)
    stl_candidates: List["StlEntry"] = field(default_factory=list)


@dataclass
class StlEntry:
    file: DriveFile
    parent_folder_name: str  # for presupported preference


def walk(client: DriveClient, root_id: str) -> List[Model]:
    models: List[Model] = []
    seen: set[str] = set()
    _visit(client, root_id, root_name="", path=[], depth=0, seen=seen, models=models)
    return models


def _visit(
    client: DriveClient,
    folder_id: str,
    root_name: str,
    path: List[str],
    depth: int,
    seen: set[str],
    models: List[Model],
) -> dict:
    """DFS post-order. Returns dict with subtree info for parent's classification.

    Returns:
        {
          "has_stl": bool,
          "models_added": list[Model] without release set
              (caller sets release if it's a group),
          "all_images": list[DriveFile] from this subtree,
          "all_stls":   list[StlEntry] from this subtree,
        }
    """
    if folder_id in seen:
        return {"has_stl": False, "models_added": [], "all_images": [], "all_stls": []}
    seen.add(folder_id)

    if depth > MAX_DEPTH:
        log.warning("max depth reached at %s", "/".join(path))
        return {"has_stl": False, "models_added": [], "all_images": [], "all_stls": []}

    direct_files: List[DriveFile] = []
    sub_folders: List[DriveFile] = []
    try:
        children = list(client.list_children(folder_id))
    except Exception as e:
        log.warning("failed to list %s (%s): %s", root_name, folder_id, e)
        return {"has_stl": False, "models_added": [], "all_images": [], "all_stls": []}

    for c in children:
        if c.is_folder:
            sub_folders.append(c)
        else:
            direct_files.append(c)

    direct_images = [f for f in direct_files if _is_image(f)]
    direct_stls = [
        StlEntry(file=f, parent_folder_name=root_name) for f in direct_files if _is_stl(f)
    ]

    sub_results = []
    for sf in sub_folders:
        r = _visit(
            client,
            sf.effective_id,
            root_name=sf.name,
            path=path + [sf.name],
            depth=depth + 1,
            seen=seen,
            models=models,
        )
        sub_results.append((sf, r))

    pending_models = [m for _, r in sub_results for m in r["models_added"]]
    sub_has_stl = any(r["has_stl"] for _, r in sub_results) or bool(direct_stls)

    if not sub_has_stl:
        if root_name:
            log.debug("skip (no stls): %s", "/".join(path))
        return {"has_stl": False, "models_added": [], "all_images": [], "all_stls": []}

    name_is_generic = _is_generic_name(root_name)
    is_root = depth == 0

    if pending_models:
        # I have non-generic descendants that already became models. I'm a GROUP.
        # Distribute my own + bubbled-up images to those models and label release.
        my_images = list(direct_images)
        for _, r in sub_results:
            my_images.extend(r["all_images"])
        for m in pending_models:
            if my_images:
                m.image_candidates.extend(my_images)
            if m.release is None and root_name and not name_is_generic and not is_root:
                m.release = root_name
        return {
            "has_stl": True,
            "models_added": pending_models,
            "all_images": [],
            "all_stls": [],
        }

    # No descendant model proposed yet. Either I become the model (if my name
    # is meaningful), or I bubble up everything I aggregated to my parent.
    all_images = list(direct_images)
    all_stls = list(direct_stls)
    for _, r in sub_results:
        all_images.extend(r["all_images"])
        all_stls.extend(r["all_stls"])

    if name_is_generic or is_root:
        # Generic container ("STL", "Bust", "Presupports", "1/10 Scale", ...)
        # — propagate up so a non-generic ancestor takes ownership.
        log.debug(
            "bubble up from %s: %d images, %d stls",
            "/".join(path) or "(root)", len(all_images), len(all_stls),
        )
        return {
            "has_stl": True,
            "models_added": [],
            "all_images": all_images,
            "all_stls": all_stls,
        }

    # I'm a non-generic folder with STLs in my subtree and no sub-models.
    # Become THE model for everything in this subtree.
    leaf = root_name
    display_name = _meaningful_name(path, leaf)
    if display_name != leaf:
        log.info(
            "display rename: %s -> %s (path=%s)",
            leaf, display_name, "/".join(path),
        )

    model = Model(
        name=leaf,
        display_name=display_name,
        folder_id=folder_id,
        folder_path=list(path),
        web_view_link=f"https://drive.google.com/drive/folders/{folder_id}",
        image_candidates=all_images,
        stl_candidates=all_stls,
    )
    models.append(model)
    log.info(
        "model: %s (images=%d, stls=%d, path=%s)",
        model.name,
        len(all_images),
        len(all_stls),
        "/".join(path),
    )
    return {
        "has_stl": True,
        "models_added": [model],
        "all_images": [],
        "all_stls": [],
    }
