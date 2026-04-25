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

# Primary hard short-circuit — strongest "this IS the cover" labels.
#   beauty shot / beauty pic
#   FinalRender (with optional separator)
#   bare "Final" as the entire base name
#   "BS" suffix followed by a number (NomNom's Beauty Shot abbreviation,
#    e.g. "BellBeast BS 01.jpg") — only when not preceded by a letter,
#    so "ABS" / "TurntableBS" don't false-match.
_BEAUTY_RE = _re.compile(
    r"beauty[\s_\-]*shot|beauty[\s_\-]*pic|"
    r"final[\s_\-]*render|^final$|"
    r"(?<![a-zA-Z])bs[\s_\-]*\d",
    _re.IGNORECASE,
)

# Secondary hard short-circuit — used only when the primary set has no
# matches. "Cover" and "Poster" are weaker hints.
_COVER_RE = _re.compile(r"(?<![a-zA-Z])cover", _re.IGNORECASE)

# Soft filter: filenames containing "final" or "render" as a word, or
# matching the folder name. These narrow the candidate pool that goes to
# colourfulness scoring — most colourful within the filtered pool wins.
# We don't auto-pick (largest) because the technical PARTS / SCALE sheets
# also tend to match these labels and used to dominate by file size.
_FINAL_RE = _re.compile(r"(?<![a-z])final|final(?![a-z])", _re.IGNORECASE)
_RENDER_RE = _re.compile(r"(?<![a-z])render|render(?![a-z])", _re.IGNORECASE)
# "Poster NN" / "Poster_NN" / "Poster.jpg" — NomNom's secondary cover
# convention. Lower precedence than the hard-pick set: only used when no
# Beauty/BS/FinalRender/Final/Cover/FolderName match exists.
_POSTER_RE = _re.compile(r"(?<![a-zA-Z])poster", _re.IGNORECASE)
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

# STL files NomNom (and similar packs) ship that are NOT meant as the
# end-user printable model — calibration prints, test cuts, work-in-progress
# meshes, demo samples. We strip these from the model's file list so the
# dropdown stays short and only shows files a user actually wants to print.
# Matched on the base name, case-insensitive, only as standalone tokens
# (so "stress_test.stl" matches but "TestifyHero.stl" doesn't).
_SEMI_PRODUCT_STL_RE = _re.compile(
    r"(?<![a-z])(?:"
    r"test|sample|demo|preview|wip|calibration|"
    r"cut[_\-\s]?test|stress[_\-\s]?test|temple|"
    r"benchmark|bench[_\-\s]?print"
    r")(?![a-z])",
    _re.IGNORECASE,
)

# "Presupported" / "PreSupports" / "Pre_Supported" anywhere in the base name.
# Used both to flag the preferred user-friendly variant and to disambiguate
# bare "supports" (which is a stand-alone supports file, NOT presupported).
_PRESUPPORTED_RE = _re.compile(
    r"pre[_\-\s]?supports?(?:ed)?",
    _re.IGNORECASE,
)

# Saturn 4 Ultra optimization marker — only the unambiguous forms.
# Generic "Saturn" is too noisy (Saturn 1/2/3 also exist, plus non-printer
# meanings); generic "Elegoo" / "12K" / "ChituBox" cover too many printers.
# We require either the full "Saturn 4 Ultra" string or the precise
# abbreviation "S4U" (also seen in Elegoo's part number EL-3D-S4U).
# Lookarounds use [A-Za-z0-9] (not \b, which treats `_` as a word char) so
# "S4U_Presupported.stl" matches but "TrissS4Ultra.stl" doesn't.
_SATURN_RE = _re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"saturn[\s_\-]*4[\s_\-]*ultra"
    r"|s4u"
    r"|el[\s_\-]?3d[\s_\-]?s4u"
    r")(?![A-Za-z0-9])",
    _re.IGNORECASE,
)

# Pre-sliced resin formats. A `.ctb` or `.goo` file is *almost certainly*
# meant for a specific printer — but we only flag it as Saturn-optimized
# when its filename or one of its ancestor folders carries an explicit
# Saturn marker, since both formats are slicer-format-compatible across
# the Elegoo / Anycubic family.
_SLICED_EXTS = (".ctb", ".goo")


def _is_beauty_shot(name: str) -> bool:
    return bool(_BEAUTY_RE.search(name))


def _series_number(filename: str) -> int:
    """Last numeric token in the filename, used to sort BS 01 ahead of
    BS 02. Files with no number sort last."""
    base = filename.rsplit(".", 1)[0]
    nums = _re.findall(r"\d+", base)
    return int(nums[-1]) if nums else 999_999


def _is_hard_pick(filename: str, model_name: str) -> bool:
    """Files that bypass scoring entirely:
      - "Beauty shot" / "cover" / "FinalRender" / "Final" / "BS NN"
        labels (via _BEAUTY_RE)
      - Filename that is a single clean proper-noun matching a token of
        the model folder name (Geralt.jpg in a Geralt folder).
    """
    base = filename.rsplit(".", 1)[0]
    if _BEAUTY_RE.search(base):
        return True
    if _PROPER_NOUN_RE.match(base) and base.lower() not in _NAME_STOPWORDS:
        if base.lower() in _name_tokens(model_name):
            return True
    return False


def _is_secondary_pick(filename: str) -> bool:
    """Secondary hard-pick — only consulted when nothing matches the
    primary set. Covers NomNom's softer cover conventions:
      - "Poster_01.jpg" / "Poster.jpg"
      - "cover.jpg" / "Foo_Cover.jpg"
    """
    base = filename.rsplit(".", 1)[0]
    return bool(_POSTER_RE.search(base) or _COVER_RE.search(base))


def _is_semi_product_stl(filename: str) -> bool:
    """True for STL/archive files that aren't end-user models —
    calibration prints, test cuts, WIP meshes, demos. These are stripped
    from the per-card file list."""
    base = filename.rsplit(".", 1)[0]
    return bool(_SEMI_PRODUCT_STL_RE.search(base))


def _is_saturn_optimized(filename: str, parent_chain: Optional[List[str]] = None) -> bool:
    """True when a file is unambiguously labelled as targeting the Elegoo
    Saturn 4 Ultra — either via its own basename or via any folder name in
    its ancestor chain (so a `Saturn 4 Ultra/Presupports/STL/foo.stl` file
    gets flagged even though its immediate parent is just `STL`).
    """
    base = filename.rsplit(".", 1)[0]
    if _SATURN_RE.search(base):
        return True
    for folder in parent_chain or []:
        if folder and _SATURN_RE.search(folder):
            return True
    return False


def _is_presupported_stl(filename: str, parent_folder_name: str) -> bool:
    """Treat a file as presupported when its parent folder name contains
    `Presupport(s/ed)` (current convention — covers `Presupports`,
    `Presupported`, `Kratos_Presupports`, ...) OR its own filename does
    (NomNom sometimes ships `Foo_Presupported.stl` mixed into a flat folder)."""
    if parent_folder_name and _PRESUPPORTED_RE.search(parent_folder_name):
        return True
    base = filename.rsplit(".", 1)[0]
    return bool(_PRESUPPORTED_RE.search(base))


def _name_tokens(s: str) -> set[str]:
    return {
        t.lower()
        for t in _re.findall(r"[A-Za-z]+", s)
        if len(t) >= 3 and t.lower() not in _NAME_STOPWORDS
    }


def _hints_for(filename: str, model_name: str) -> list[str]:
    """Return the list of hint labels that match the filename. Empty list
    means it'll only enter the scoring pool when there are no hinted files."""
    base = filename.rsplit(".", 1)[0]
    hits: list[str] = []
    if _BEAUTY_RE.search(base):
        hits.append("beauty")
    if _FINAL_RE.search(base):
        hits.append("final")
    if _RENDER_RE.search(base):
        hits.append("render")
    if _PROPER_NOUN_RE.match(base) and base.lower() not in _NAME_STOPWORDS:
        hits.append("propnoun")
    if _name_tokens(base) & _name_tokens(model_name):
        hits.append("foldername")
    return hits


def _has_hint(filename: str, model_name: str) -> bool:
    return bool(_hints_for(filename, model_name))


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

    # Hard short-circuit (primary): explicit cover labels (Beauty shot /
    # cover / FinalRender / Final / BS NN / FolderName.jpg) bypass scoring.
    # Within the set we prefer the lowest series number ("BS 01" beats
    # "BS 02") with file size as a tiebreaker.
    hard = [f for f in model.image_candidates if _is_hard_pick(f.name, model.name)]
    if hard:
        hard.sort(key=lambda f: (_series_number(f.name), -(f.size or 0)))
        chosen = hard[0]
        log.info("[%s] hard pick: %s — using directly", model.name, chosen.name)
        try:
            data = _fetch_image(client, chosen)
            pil = Image.open(io.BytesIO(data))
            pil = ImageOps.exif_transpose(pil).convert("RGB")
            return ScoredImage(file=chosen, score=999.0, raw_bytes=data, pil_image=pil)
        except Exception as e:
            log.warning("[%s] hard pick %s failed (%s) — falling back to scoring", model.name, chosen.name, e)

    # Hard short-circuit (secondary): "cover" / "Poster NN" / "Poster.jpg"
    # — only checked when nothing matches the primary set above.
    secondary = [f for f in model.image_candidates if _is_secondary_pick(f.name)]
    if secondary:
        secondary.sort(key=lambda f: (_series_number(f.name), -(f.size or 0)))
        chosen = secondary[0]
        log.info("[%s] secondary pick: %s — using directly", model.name, chosen.name)
        try:
            data = _fetch_image(client, chosen)
            pil = Image.open(io.BytesIO(data))
            pil = ImageOps.exif_transpose(pil).convert("RGB")
            return ScoredImage(file=chosen, score=999.0, raw_bytes=data, pil_image=pil)
        except Exception as e:
            log.warning("[%s] secondary %s failed (%s) — falling back to scoring", model.name, chosen.name, e)

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
    """Return end-user STL/archive files sorted: presupported variants
    first (largest first), then the rest (largest first). Semi-products
    (test/sample/demo/preview/WIP/calibration prints) are dropped — those
    are tooling, not characters the user wants to browse to. Empty list
    means no usable STLs in this model."""
    n = len(model.stl_candidates)
    if n == 0:
        log.warning("[%s] no STL files in subtree", model.name)
        return []

    kept: List[StlEntry] = []
    dropped: List[str] = []
    for s in model.stl_candidates:
        if _is_semi_product_stl(s.file.name):
            dropped.append(s.file.name)
        else:
            kept.append(s)

    if dropped:
        log.info(
            "[%s] dropped %d semi-product file(s): %s",
            model.name, len(dropped), ", ".join(dropped),
        )

    if not kept:
        log.warning(
            "[%s] all %d STL(s) classified as semi-product — keeping anyway",
            model.name, n,
        )
        kept = list(model.stl_candidates)

    presupported = sorted(
        (s for s in kept if _is_presupported_stl(s.file.name, s.parent_folder_name)),
        key=lambda s: (s.file.size or 0),
        reverse=True,
    )
    rest = sorted(
        (s for s in kept if not _is_presupported_stl(s.file.name, s.parent_folder_name)),
        key=lambda s: (s.file.size or 0),
        reverse=True,
    )
    ordered = presupported + rest
    log.info(
        "[%s] %d STL(s) kept: %d presupported, %d other; first = %s (%s)",
        model.name, len(kept), len(presupported), len(rest),
        ordered[0].file.name,
        f"{(ordered[0].file.size or 0)/1_000_000:.1f}MB",
    )
    return ordered
