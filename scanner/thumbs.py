"""Generate gallery thumbnails from already-loaded PIL images."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from PIL import Image, ImageOps

THUMB_MAX_SIZE = 600
THUMB_QUALITY = 82


def _short_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:10]


def _slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or "model"


def thumb_path(out_dir: Path, model_name: str, source_file_id: str) -> Path:
    fname = f"{_slug(model_name)}-{_short_hash(source_file_id)}.jpg"
    return out_dir / fname


def write_thumb(image: Image.Image, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    img = ImageOps.exif_transpose(image).convert("RGB")
    img.thumbnail((THUMB_MAX_SIZE, THUMB_MAX_SIZE))
    img.save(dest, format="JPEG", quality=THUMB_QUALITY, optimize=True)
