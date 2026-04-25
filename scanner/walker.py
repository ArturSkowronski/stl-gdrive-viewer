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
from dataclasses import dataclass, field
from typing import List, Optional

from .drive import DriveClient, DriveFile

log = logging.getLogger(__name__)

IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
STL_EXTS = (".stl",)
MAX_DEPTH = 6


def _is_stl(f: DriveFile) -> bool:
    return f.effective_mime != "application/vnd.google-apps.folder" and f.name.lower().endswith(
        STL_EXTS
    )


def _is_image(f: DriveFile) -> bool:
    return f.effective_mime in IMAGE_MIMES


@dataclass
class Model:
    name: str
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

    if pending_models:
        # I am a GROUP. Set my name as `release` on any pending model that
        # doesn't already have one (nearest-ancestor wins).
        for m in pending_models:
            if m.release is None and root_name:
                m.release = root_name
        # Propagate upward unchanged.
        return {
            "has_stl": sub_has_stl,
            "models_added": pending_models,
            "all_images": [],
            "all_stls": [],
        }

    if sub_has_stl:
        # I am a MODEL. Aggregate all images and STLs from my subtree.
        all_images = list(direct_images)
        all_stls = list(direct_stls)
        for _, r in sub_results:
            all_images.extend(r["all_images"])
            all_stls.extend(r["all_stls"])

        model = Model(
            name=root_name or "(root)",
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

    # No STLs anywhere — skip.
    if root_name:
        log.debug("skip (no stls): %s", "/".join(path))
    return {"has_stl": False, "models_added": [], "all_images": [], "all_stls": []}
