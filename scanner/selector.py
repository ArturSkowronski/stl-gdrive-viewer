"""Pick the best 'painted model' image and the right STL from candidates.

Image score uses Hasler & Süsstrunk colorfulness + HSV mean saturation —
painted minis score high, gray STL renders / box art / promo shots low.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
from PIL import Image, ImageOps

from .drive import DriveClient, DriveFile
from .walker import Model, StlEntry

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
SCORE_RESIZE_TO = 256  # downscale before scoring — speed
COLORFULNESS_WEIGHT = 0.7
SATURATION_WEIGHT = 0.3


@dataclass
class ScoredImage:
    file: DriveFile
    score: float
    raw_bytes: bytes  # cached, reused for thumbnail generation
    pil_image: Image.Image


def _colorfulness(arr: np.ndarray) -> float:
    """Hasler & Süsstrunk 2003 colorfulness metric on an RGB uint8 array."""
    r = arr[..., 0].astype(np.float32)
    g = arr[..., 1].astype(np.float32)
    b = arr[..., 2].astype(np.float32)
    rg = r - g
    yb = 0.5 * (r + g) - b
    std_root = math.sqrt(float(rg.std() ** 2 + yb.std() ** 2))
    mean_root = math.sqrt(float(rg.mean() ** 2 + yb.mean() ** 2))
    return std_root + 0.3 * mean_root


def _mean_saturation(rgb: np.ndarray) -> float:
    """Average HSV saturation in [0, 1] of a downscaled RGB image."""
    img = Image.fromarray(rgb).convert("HSV")
    hsv = np.asarray(img, dtype=np.float32)
    return float(hsv[..., 1].mean()) / 255.0


def score_image_bytes(data: bytes) -> tuple[float, Image.Image]:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    small = img.copy()
    small.thumbnail((SCORE_RESIZE_TO, SCORE_RESIZE_TO))
    arr = np.asarray(small)
    cf = _colorfulness(arr)
    sat = _mean_saturation(arr)
    # Hasler-Süsstrunk values typically fall in 0..100 range; normalize
    # softly to ~[0,1] without clipping the top.
    cf_norm = cf / 100.0
    score = COLORFULNESS_WEIGHT * cf_norm + SATURATION_WEIGHT * sat
    return score, img


def pick_cover(client: DriveClient, model: Model) -> Optional[ScoredImage]:
    """Download each candidate image, score it, return the best one."""
    candidates: List[ScoredImage] = []
    for f in model.image_candidates:
        if f.size is not None and f.size > MAX_IMAGE_BYTES:
            log.debug("skip large image: %s (%d bytes)", f.name, f.size)
            continue
        try:
            data = client.download_bytes(f.id, max_bytes=MAX_IMAGE_BYTES)
        except Exception as e:
            log.warning("download failed for %s: %s", f.name, e)
            continue
        try:
            score, pil = score_image_bytes(data)
        except Exception as e:
            log.warning("scoring failed for %s: %s", f.name, e)
            continue
        candidates.append(ScoredImage(file=f, score=score, raw_bytes=data, pil_image=pil))
        log.debug("score %.3f for %s in model %s", score, f.name, model.name)

    if not candidates:
        return None

    def tiebreak_key(c: ScoredImage) -> tuple[float, int]:
        res = (c.file.width or 0) * (c.file.height or 0)
        return (c.score, res)

    candidates.sort(key=tiebreak_key, reverse=True)
    return candidates[0]


def pick_stl(model: Model) -> Optional[StlEntry]:
    """Pick the most useful STL: presupported preferred, then largest."""
    if not model.stl_candidates:
        return None

    presupported = [
        s for s in model.stl_candidates if "presupported" in s.parent_folder_name.lower()
    ]
    pool = presupported if presupported else model.stl_candidates
    pool_sorted = sorted(
        pool,
        key=lambda s: (s.file.size or 0),
        reverse=True,
    )
    return pool_sorted[0]
