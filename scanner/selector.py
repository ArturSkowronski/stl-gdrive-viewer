"""Pick the best 'painted model' image and the right STL from candidates.

Image score uses Hasler & Süsstrunk colorfulness + HSV mean saturation —
painted minis score high, gray STL renders / box art / promo shots low.
"""

from __future__ import annotations

import io
import logging
import math
import re as _re
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

# Cap how many images we score per model. NomNom often ships 10-30 renders
# per character; downloading them all hammers the Drive API and rarely
# changes the verdict. We rank by file size first (proxy for "interesting"
# images — promo art is usually larger than thumbnails) and take the top N.
MAX_SCORED_PER_MODEL = 6

# Priority tiers for "obvious cover" filenames. Lower number = higher
# priority; the best tier wins, with file size as tiebreaker.
_BEAUTY_RE = _re.compile(r"beauty[\s_\-]*(shot|pic)", _re.IGNORECASE)
_COVER_RE = _re.compile(r"^cover|[\s_\-]cover(?![a-z])", _re.IGNORECASE)
_EDITED_RE = _re.compile(r"(?<![a-z])edited|edited(?![a-z])", _re.IGNORECASE)
_FINAL_RE = _re.compile(r"(?<![a-z])final|final(?![a-z])", _re.IGNORECASE)

_NAME_STOPWORDS = {
    "stl", "stls", "bust", "busts", "scale", "miniature", "miniatures",
    "render", "renders", "image", "images", "model", "the", "and", "for",
    "from", "presupported", "unsupported",
    "parts", "wip", "test", "lore", "raw", "photo", "photos", "preview",
    "turntable", "supported", "unsupports", "presupports",
}

# Single capitalized proper-noun filename like "Triss.jpg" or "Geralt.jpg"
# — a clean single token, not a technical label, treated as a dedicated cover.
_PROPER_NOUN_RE = _re.compile(r"^[A-Z][a-zA-Z]{2,}$")

# Soft-hint bonuses added to the colourfulness score so a hinted file
# preferred over an unhinted one when colour is similar, but a clearly
# colourful unhinted image still wins (raw scores can reach ~1.5+ for
# painted minis vs ~0.05 for greyscale renders, so 0.4 is a meaningful
# but not dominating nudge).
_TIER_BONUS = {3: 0.30, 4: 0.30, 5: 0.40, 6: 0.20, 999: 0.0}


def _name_tokens(s: str) -> set[str]:
    return {
        t.lower()
        for t in _re.findall(r"[A-Za-z]+", s)
        if len(t) >= 3 and t.lower() not in _NAME_STOPWORDS
    }


def _cover_priority(filename: str, model_name: str) -> int:
    """Lower = better. 999 means no obvious-cover signal."""
    base = filename.rsplit(".", 1)[0]
    if _BEAUTY_RE.search(base):
        return 1
    if _COVER_RE.search(base):
        return 2
    if _EDITED_RE.search(base):
        return 3
    if _FINAL_RE.search(base):
        return 4
    # "Triss.jpg", "Geralt.jpg" — a clean single capitalized proper noun
    # the artist clearly intended as the showcase image.
    if _PROPER_NOUN_RE.match(base) and base.lower() not in _NAME_STOPWORDS:
        return 5
    if _name_tokens(base) & _name_tokens(model_name):
        return 6
    return 999


def _series_number(filename: str) -> int:
    """Last numeric token in the filename — proxy for series ordinal.
    `Queen-of-Blades-1_edited.jpg` -> 1, `Asuka_v2_edited.jpg` -> 2.
    Files with no number sort after numbered ones (default 999_999)."""
    base = filename.rsplit(".", 1)[0]
    nums = _re.findall(r"\d+", base)
    return int(nums[-1]) if nums else 999_999


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


def _fetch_image(client: DriveClient, file) -> bytes:
    """Try the CDN-thumbnail fast path first, fall back to full download."""
    data = client.fetch_thumbnail(file, size=1024)
    if data:
        return data
    return client.download_bytes(file.id, max_bytes=DOWNLOAD_HARD_CAP)


def pick_cover(client: DriveClient, model: Model) -> Optional[ScoredImage]:
    """Download each candidate image, score it, return the best one.

    Falls back to the first successfully-loaded image if scoring fails for
    every candidate — better to show *some* picture than skip the model.
    """
    n_total = len(model.image_candidates)
    if n_total == 0:
        log.info("[%s] 0 image candidates", model.name)
        return None

    # Priority tiers from filenames: 1=beauty, 2=cover (hard short-circuit),
    # 3=edited, 4=final, 5=proper-noun, 6=folder-name match (soft hints —
    # add a bonus during scoring so they're preferred but a clearly more
    # colourful image without a hint can still win).
    ranked = [
        (f, _cover_priority(f.name, model.name)) for f in model.image_candidates
    ]

    # Hard short-circuit: explicit "Beauty shot" / "cover" filenames win
    # without scoring. Largest matching one (lowest series number first).
    hard = sorted(
        ((f, p) for f, p in ranked if p <= 2),
        key=lambda x: (x[1], _series_number(x[0].name), -(x[0].size or 0)),
    )
    if hard:
        chosen, tier = hard[0]
        log.info(
            "[%s] hard cover (tier %d): %s — using directly",
            model.name, tier, chosen.name,
        )
        try:
            data = _fetch_image(client, chosen)
            pil = Image.open(io.BytesIO(data))
            pil = ImageOps.exif_transpose(pil).convert("RGB")
            return ScoredImage(file=chosen, score=999.0, raw_bytes=data, pil_image=pil)
        except Exception as e:
            log.warning("[%s] hard %s failed (%s) — falling back to scoring", model.name, chosen.name, e)

    # Soft hints (3-6) get a score bonus, then everything goes through
    # colourfulness scoring. Build the candidate pool: all hinted files
    # first, then top non-hinted by size, capped at MAX_SCORED_PER_MODEL.
    hinted = sorted(
        ((f, p) for f, p in ranked if 3 <= p <= 6),
        key=lambda x: (x[1], _series_number(x[0].name), -(x[0].size or 0)),
    )
    unhinted = sorted(
        ((f, 999) for f, p in ranked if p == 999),
        key=lambda x: -(x[0].size or 0),
    )
    pool = (hinted + unhinted)[:MAX_SCORED_PER_MODEL]
    n = len(pool)
    log.info(
        "[%s] %d image candidate(s), pool of %d: %s",
        model.name,
        n_total,
        n,
        ", ".join(f"{f.name}(t{p})" for f, p in pool) or "(none)",
    )

    scored: List[tuple[ScoredImage, int, float]] = []  # (img, tier, combined)
    fallback: Optional[ScoredImage] = None

    for f, tier in pool:
        size_str = f"{f.size/1_000_000:.1f}MB" if f.size else "?MB"
        try:
            data = _fetch_image(client, f)
        except Exception as e:
            log.warning("[%s]   reject %s (%s): download — %s", model.name, f.name, size_str, e)
            continue

        try:
            raw_score, pil = score_image_bytes(data)
            bonus = _TIER_BONUS.get(tier, 0.0)
            combined = raw_score + bonus
            si = ScoredImage(file=f, score=raw_score, raw_bytes=data, pil_image=pil)
            scored.append((si, tier, combined))
            log.info(
                "[%s]   ok     %s (%s) score=%.3f tier=%d combined=%.3f",
                model.name, f.name, size_str, raw_score, tier, combined,
            )
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
        scored.sort(key=lambda x: (-x[2], -(x[0].file.width or 0) * (x[0].file.height or 0)))
        chosen_img, tier, combined = scored[0]
        log.info(
            "[%s] picked cover %s (tier=%d combined=%.3f, %d/%d scored)",
            model.name, chosen_img.file.name, tier, combined, len(scored), n,
        )
        return chosen_img

    if fallback:
        log.info("[%s] picked fallback cover %s (no scoring succeeded)", model.name, fallback.file.name)
    else:
        log.warning("[%s] no cover possible — all %d candidates failed", model.name, n)
    return fallback


def pick_stls(model: Model) -> List[StlEntry]:
    """Return all STLs sorted: presupported variants first (largest first),
    then the rest (largest first). Empty list means no STLs in this model."""
    n = len(model.stl_candidates)
    if n == 0:
        log.warning("[%s] no STL files in subtree", model.name)
        return []

    def _is_presupported(s: StlEntry) -> bool:
        return "presupported" in s.parent_folder_name.lower()

    presupported = sorted(
        (s for s in model.stl_candidates if _is_presupported(s)),
        key=lambda s: (s.file.size or 0),
        reverse=True,
    )
    rest = sorted(
        (s for s in model.stl_candidates if not _is_presupported(s)),
        key=lambda s: (s.file.size or 0),
        reverse=True,
    )
    ordered = presupported + rest
    log.info(
        "[%s] %d STL(s): %d presupported, %d other; first = %s (%s)",
        model.name, n, len(presupported), len(rest),
        ordered[0].file.name,
        f"{(ordered[0].file.size or 0)/1_000_000:.1f}MB",
    )
    return ordered
