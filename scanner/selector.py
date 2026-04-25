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

# Beauty/cover hard short-circuit: NomNom's explicit cover label —
# largest matching file wins, no scoring needed.
_BEAUTY_RE = _re.compile(r"beauty[\s_\-]*shot|beauty[\s_\-]*pic|^cover", _re.IGNORECASE)

# Soft filter: filenames containing "final" or "render" as a word, or
# matching the folder name. These narrow the candidate pool that goes to
# colourfulness scoring — most colourful within the filtered pool wins.
# We don't auto-pick (largest) because the technical PARTS / SCALE sheets
# also tend to match these labels and used to dominate by file size.
_FINAL_RE = _re.compile(r"(?<![a-z])final|final(?![a-z])", _re.IGNORECASE)
_RENDER_RE = _re.compile(r"(?<![a-z])render|render(?![a-z])", _re.IGNORECASE)
# Clean single-word filename like Triss.jpg, Geralt.jpg — a deliberate
# character-name file. Match only the bare base, ≥3 letters, no digits.
_PROPER_NOUN_RE = _re.compile(r"^[A-Z][a-zA-Z]{2,}$")

_NAME_STOPWORDS = {
    "stl", "stls", "bust", "busts", "scale", "miniature", "miniatures",
    "render", "renders", "image", "images", "model", "the", "and", "for",
    "from", "presupported", "unsupported",
    "parts", "wip", "test", "lore", "raw", "photo", "photos", "preview",
    "turntable", "supported",
}


def _is_beauty_shot(name: str) -> bool:
    return bool(_BEAUTY_RE.search(name))


def _name_tokens(s: str) -> set[str]:
    return {
        t.lower()
        for t in _re.findall(r"[A-Za-z]+", s)
        if len(t) >= 3 and t.lower() not in _NAME_STOPWORDS
    }


def _has_hint(filename: str, model_name: str) -> bool:
    """True if a filename hints this is a labelled candidate:
      - contains 'final' or 'render' as a word
      - is a clean single proper-noun file (Triss.jpg, Geralt.jpg)
      - shares a meaningful token with the folder name
    """
    base = filename.rsplit(".", 1)[0]
    if _FINAL_RE.search(base) or _RENDER_RE.search(base):
        return True
    if _PROPER_NOUN_RE.match(base) and base.lower() not in _NAME_STOPWORDS:
        return True
    return bool(_name_tokens(base) & _name_tokens(model_name))


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

    # Beauty shots (NomNom's own cover art) bypass scoring — pick the
    # largest matching file and use it directly.
    beauty = [f for f in model.image_candidates if _is_beauty_shot(f.name)]
    if beauty:
        beauty.sort(key=lambda f: f.size or 0, reverse=True)
        chosen = beauty[0]
        log.info("[%s] beauty shot found: %s — using it as cover", model.name, chosen.name)
        try:
            data = _fetch_image(client, chosen)
            pil = Image.open(io.BytesIO(data))
            pil = ImageOps.exif_transpose(pil).convert("RGB")
            return ScoredImage(file=chosen, score=999.0, raw_bytes=data, pil_image=pil)
        except Exception as e:
            log.warning("[%s] beauty shot %s failed (%s) — falling back to scoring", model.name, chosen.name, e)

    # Filter pool: if any filenames carry a hint (final / render / folder
    # name token), score only those — bias toward labelled images. If none,
    # score everything. The most colourful image inside the pool wins.
    hinted = [f for f in model.image_candidates if _has_hint(f.name, model.name)]
    pool = hinted if hinted else list(model.image_candidates)
    pool_label = "hinted" if hinted else "all"

    # Score at most MAX_SCORED_PER_MODEL — pick the largest first since
    # promo art / beauty shots are usually larger than icon-style thumbs.
    candidates = sorted(
        pool, key=lambda f: f.size or 0, reverse=True
    )[:MAX_SCORED_PER_MODEL]
    n = len(candidates)
    log.info(
        "[%s] %d total, %d in %s pool, scoring %d: %s",
        model.name,
        n_total,
        len(pool),
        pool_label,
        n,
        ", ".join(f.name for f in candidates) or "(none)",
    )

    scored: List[ScoredImage] = []
    fallback: Optional[ScoredImage] = None

    for f in candidates:
        size_str = f"{f.size/1_000_000:.1f}MB" if f.size else "?MB"
        try:
            data = _fetch_image(client, f)
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
