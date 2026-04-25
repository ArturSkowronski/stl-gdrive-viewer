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

# Hard cap to keep memory bounded — anything sane stays well under this.
DOWNLOAD_HARD_CAP = 100 * 1024 * 1024
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
    """Download each candidate image, score it, return the best one.

    Falls back to the first successfully-loaded image if scoring fails for
    every candidate — better to show *some* picture than skip the model.
    """
    n = len(model.image_candidates)
    log.info(
        "[%s] %d image candidate(s): %s",
        model.name,
        n,
        ", ".join(f.name for f in model.image_candidates) or "(none)",
    )
    if n == 0:
        return None

    scored: List[ScoredImage] = []
    fallback: Optional[ScoredImage] = None

    for f in model.image_candidates:
        size_str = f"{f.size/1_000_000:.1f}MB" if f.size else "?MB"
        try:
            data = client.download_bytes(f.id, max_bytes=DOWNLOAD_HARD_CAP)
        except Exception as e:
            log.warning("[%s]   reject %s (%s): download — %s", model.name, f.name, size_str, e)
            continue

        try:
            score, pil = score_image_bytes(data)
            scored.append(ScoredImage(file=f, score=score, raw_bytes=data, pil_image=pil))
            log.info("[%s]   ok     %s (%s) score=%.3f", model.name, f.name, size_str, score)
        except Exception as e:
            log.warning(
                "[%s]   reject %s (%s): scoring — %s (kept as fallback)",
                model.name, f.name, size_str, e,
            )
            if fallback is None:
                try:
                    pil = Image.open(io.BytesIO(data))
                    pil = ImageOps.exif_transpose(pil).convert("RGB")
                    fallback = ScoredImage(file=f, score=0.0, raw_bytes=data, pil_image=pil)
                except Exception as e2:
                    log.warning("[%s]   fallback decode failed for %s: %s", model.name, f.name, e2)

    if scored:
        scored.sort(
            key=lambda c: (c.score, (c.file.width or 0) * (c.file.height or 0)),
            reverse=True,
        )
        chosen = scored[0]
        log.info(
            "[%s] picked cover %s (score=%.3f, %d/%d scored)",
            model.name, chosen.file.name, chosen.score, len(scored), n,
        )
        return chosen

    if fallback:
        log.info("[%s] picked fallback cover %s (no scoring succeeded)", model.name, fallback.file.name)
    else:
        log.warning("[%s] no cover possible — all %d candidates failed", model.name, n)
    return fallback


def pick_stl(model: Model) -> Optional[StlEntry]:
    """Pick the most useful STL: presupported preferred, then largest."""
    n = len(model.stl_candidates)
    if n == 0:
        log.warning("[%s] no STL files in subtree", model.name)
        return None

    presupported = [
        s for s in model.stl_candidates if "presupported" in s.parent_folder_name.lower()
    ]
    pool = presupported if presupported else model.stl_candidates
    pool_label = "presupported" if presupported else "any"
    pool_sorted = sorted(
        pool,
        key=lambda s: (s.file.size or 0),
        reverse=True,
    )
    chosen = pool_sorted[0]
    log.info(
        "[%s] picked STL %s (%s, %d/%d candidates from '%s' pool)",
        model.name,
        chosen.file.name,
        f"{(chosen.file.size or 0)/1_000_000:.1f}MB",
        1, len(pool), pool_label,
    )
    return chosen
