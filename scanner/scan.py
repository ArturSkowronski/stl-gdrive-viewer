"""Entry point: walks Drive, picks covers, writes manifest + thumbs."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Support running as a script (`python scanner/scan.py`) or as a module
# (`python -m scanner.scan`). When invoked as a script, the package root must
# be on sys.path.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scanner.drive import DriveClient  # noqa: E402
    from scanner.selector import (  # noqa: E402
        pick_cover, pick_stls, score_image_bytes, _hints_for, _fetch_image,
        _is_presupported_stl, _is_saturn_optimized,
    )
    from scanner.thumbs import thumb_path, write_thumb  # noqa: E402
    from scanner.walker import walk  # noqa: E402
else:
    from .drive import DriveClient
    from .selector import (
        pick_cover, pick_stls, score_image_bytes, _hints_for, _fetch_image,
        _is_presupported_stl, _is_saturn_optimized,
    )
    from .thumbs import thumb_path, write_thumb
    from .walker import walk


def _stl_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def _run_analyze(client, models, csv_path: Path, limit: Optional[int]) -> int:
    """Score every image candidate of every model and dump a CSV row per
    candidate so the cover-selection logic can be audited end-to-end.

    Columns: model, release, file, size_mb, hints, score, in_pool, picked.
    """
    import csv
    import io as _io
    from PIL import Image, ImageOps

    if limit:
        models = models[:limit]

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for m in models:
        cands = list(m.image_candidates)
        # Identify the "in_pool" set the same way pick_cover does.
        hints_per_file = {f.id: _hints_for(f.name, m.name) for f in cands}
        hinted = [f for f in cands if hints_per_file[f.id]]
        pool = hinted if hinted else cands

        # Score everything (so the audit shows scores for non-pool files too).
        scored: dict[str, float] = {}
        for f in cands:
            try:
                data = _fetch_image(client, f)
                score, _ = score_image_bytes(data)
                scored[f.id] = score
            except Exception as e:
                logging.warning("analyze: %s/%s scoring failed: %s", m.name, f.name, e)

        # Reproduce the pick: highest score within pool (size as tiebreaker
        # already handled by pick_cover via candidate ordering — for the
        # audit we just take max score in pool).
        picked_id: Optional[str] = None
        if pool:
            scored_pool = [(f, scored.get(f.id, -1.0)) for f in pool]
            scored_pool.sort(
                key=lambda x: (x[1], (x[0].width or 0) * (x[0].height or 0)),
                reverse=True,
            )
            picked_id = scored_pool[0][0].id

        for f in cands:
            rows.append({
                "model": m.display_name,
                "raw_folder": m.name,
                "release": m.release or "",
                "file": f.name,
                "size_mb": round((f.size or 0) / 1_000_000, 2),
                "hints": "|".join(hints_per_file[f.id]) or "-",
                "score": (
                    f"{scored[f.id]:.4f}" if f.id in scored else "ERR"
                ),
                "in_pool": "Y" if f in pool else "N",
                "picked": "Y" if f.id == picked_id else "N",
            })

    if not rows:
        logging.warning("analyze: no candidates to score")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [
            "model", "raw_folder", "release", "file", "size_mb", "hints",
            "score", "in_pool", "picked",
        ])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    logging.warning(
        "analyze: wrote %d rows for %d models to %s", len(rows), len(models), csv_path,
    )
    return 0


def _load_existing_manifest(path: Path) -> dict[str, dict]:
    """Read a previous manifest if it exists; return {folder_id: entry}.

    Used by --incremental to skip cover-fetching + thumb regeneration for
    models we've already processed. A missing or unparseable file means
    "no prior state" — caller falls back to a full scan.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logging.warning("could not load existing manifest at %s: %s", path, e)
        return {}
    return {m["id"]: m for m in data.get("models", []) if m.get("id")}


def _prune_orphan_thumbs(thumbs_dir: Path, manifest_models: list) -> int:
    """Delete thumb files no longer referenced by any manifest entry.

    Each manifest model's `thumb` is stored as a relative path
    (`thumbs/<filename>`); we look up by basename so the prune works
    regardless of how the path was joined. Returns the number deleted.
    """
    if not thumbs_dir.exists():
        return 0
    referenced: set[str] = set()
    for m in manifest_models:
        thumb_rel = m.get("thumb")
        if thumb_rel:
            referenced.add(Path(thumb_rel).name)
    removed = 0
    for fp in thumbs_dir.iterdir():
        if fp.is_file() and fp.name not in referenced:
            try:
                fp.unlink()
                removed += 1
                logging.info("pruned orphan thumb: %s", fp.name)
            except OSError as e:
                logging.warning("could not remove %s: %s", fp, e)
    return removed


def _release_sort_key(release: Optional[str]) -> tuple:
    if not release:
        return (1, "")  # null releases sorted last
    # Try to parse "Month Year" → datetime for chronological-ish sort.
    try:
        dt = datetime.strptime(release, "%B %Y")
        return (0, -dt.toordinal(), release)
    except ValueError:
        return (0, 0, release)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="Root Drive folder ID")
    parser.add_argument(
        "--out", default="site/manifest.json", help="Output manifest path"
    )
    parser.add_argument(
        "--thumbs", default="site/thumbs", help="Output thumbnail directory"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Process at most N models (debug)"
    )
    parser.add_argument(
        "--verbose", "-v", action="count", default=0, help="-v info, -vv debug"
    )
    parser.add_argument(
        "--analyze",
        metavar="CSV",
        help=(
            "Diagnostic mode: instead of writing a manifest, scan every "
            "model + image candidate, score them all, and write a CSV to "
            "this path with hints, scores, and which file would be picked. "
            "Use this to audit cover selection across the whole catalogue."
        ),
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "Skip cover-fetching + thumbnail generation for models whose "
            "folder_id already appears in the existing manifest at --out. "
            "New models are processed normally, deleted models are pruned, "
            "orphaned thumb files are cleaned. Use this for daily refreshes; "
            "use a full scan (no --incremental) when you want covers and "
            "STL lists to re-pick from scratch."
        ),
    )
    args = parser.parse_args()

    level = logging.WARNING
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose == 1:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    out_path = Path(args.out)
    thumbs_dir = Path(args.thumbs)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    client = DriveClient()
    logging.info("walking Drive from root %s", args.root)
    models = walk(client, args.root)
    logging.info("found %d candidate models", len(models))

    if args.analyze:
        return _run_analyze(client, models, Path(args.analyze), args.limit)

    # Merge models that ended up with the same (release, display_name) — e.g.
    # "Kratos_STL" and "Kratos_Presupport" both displaying as "Kratos" under
    # the same release. STLs and images are unioned by file id to avoid dupes.
    merged: dict[tuple, "object"] = {}
    for m in models:
        key = (m.release or "", m.display_name)
        if key in merged:
            existing = merged[key]
            existing.image_candidates.extend(m.image_candidates)
            existing.stl_candidates.extend(m.stl_candidates)
        else:
            merged[key] = m

    def _dedupe(items, file_id):
        seen, out = set(), []
        for x in items:
            k = file_id(x)
            if k not in seen:
                seen.add(k)
                out.append(x)
        return out

    merged_models = []
    for m in merged.values():
        before_imgs, before_stls = len(m.image_candidates), len(m.stl_candidates)
        m.image_candidates = _dedupe(m.image_candidates, lambda f: f.id)
        m.stl_candidates = _dedupe(m.stl_candidates, lambda s: s.file.id)
        if before_imgs != len(m.image_candidates) or before_stls != len(m.stl_candidates):
            logging.info(
                "merged duplicates for %s: images %d->%d, stls %d->%d",
                m.display_name, before_imgs, len(m.image_candidates),
                before_stls, len(m.stl_candidates),
            )
        merged_models.append(m)
    if len(merged_models) != len(models):
        logging.info("merged %d -> %d models", len(models), len(merged_models))
    models = merged_models

    if args.limit:
        models = models[: args.limit]

    # Incremental mode: load the previous manifest and short-circuit any
    # model whose folder_id we've already processed. The cached entry
    # (display_name, release, thumb, stls, saturn_optimized) is copied
    # verbatim — no Drive image fetch, no PIL pass.
    cached: dict[str, dict] = (
        _load_existing_manifest(out_path) if args.incremental else {}
    )
    if args.incremental:
        logging.warning(
            "incremental: loaded %d cached model(s) from %s",
            len(cached), out_path,
        )

    manifest_models = []
    skipped_no_cover: list[str] = []
    skipped_no_stl: list[str] = []
    crashed: list[str] = []
    carried_forward = 0
    newly_processed = 0
    for m in models:
        try:
            if m.folder_id in cached:
                # Trust the previous run — caller asked for incremental,
                # the contract is "once indexed, doesn't change."
                manifest_models.append(cached[m.folder_id])
                carried_forward += 1
                continue
            cover = pick_cover(client, m)
            stls = pick_stls(m)
            if not stls:
                logging.warning("skip %s — no STL", m.name)
                skipped_no_stl.append(m.name)
                continue
            newly_processed += 1

            if cover:
                thumb_dest = thumb_path(thumbs_dir, m.name, cover.file.id)
                write_thumb(cover.pil_image, thumb_dest)
                thumb_rel: Optional[str] = thumb_dest.relative_to(out_path.parent).as_posix()
            else:
                logging.warning("%s has no cover — emitting card without thumbnail", m.name)
                skipped_no_cover.append(m.name)
                thumb_rel = None

            stl_entries = []
            any_saturn = False
            for s in stls:
                # Folder chain to scan for Saturn markers: walker's
                # parent_chain (every ancestor down to immediate parent)
                # plus the model's own folder_path + leaf name (in case the
                # marker sits on the model folder itself).
                chain = list(s.parent_chain) + list(m.folder_path) + [m.name]
                saturn = _is_saturn_optimized(s.file.name, chain)
                any_saturn = any_saturn or saturn
                stl_entries.append({
                    "file_id": s.file.id,
                    "name": s.file.name,
                    "size": s.file.size,
                    "view_url": s.file.web_view_link or _stl_view_url(s.file.id),
                    "presupported": _is_presupported_stl(
                        s.file.name, s.parent_folder_name
                    ),
                    "saturn_optimized": saturn,
                })

            manifest_models.append(
                {
                    "id": m.folder_id,
                    "name": m.display_name,
                    "release": m.release,
                    "folder_url": m.web_view_link,
                    "thumb": thumb_rel,
                    "saturn_optimized": any_saturn,
                    "stls": stl_entries,
                }
            )
        except Exception as e:
            logging.exception("crash processing model %s: %s — keeping run alive", m.name, e)
            crashed.append(m.name)
            continue

    manifest_models.sort(
        key=lambda mm: (_release_sort_key(mm["release"]), mm["name"].lower())
    )

    releases_seen = []
    for mm in manifest_models:
        r = mm["release"]
        if r and r not in releases_seen:
            releases_seen.append(r)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "releases": releases_seen,
        "models": manifest_models,
    }
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    # In incremental mode, prune thumb files that no longer back any
    # manifest entry — models removed from Drive (or merged into others)
    # would otherwise leak files into the cache forever.
    if args.incremental:
        pruned = _prune_orphan_thumbs(thumbs_dir, manifest_models)
        seen_ids = {mm["id"] for mm in manifest_models}
        dropped = [eid for eid in cached if eid not in seen_ids]
        logging.warning(
            "incremental summary: %d carried forward, %d new, %d dropped, %d orphan thumbs pruned",
            carried_forward, newly_processed, len(dropped), pruned,
        )

    # Final summary so the cause of any drop-off is obvious in CI logs.
    total = len(models)
    written = len(manifest_models)
    logging.warning(
        "summary: %d candidates -> %d written, %d without cover (still shown), %d skipped (no STL), %d crashed",
        total, written, len(skipped_no_cover), len(skipped_no_stl), len(crashed),
    )
    if skipped_no_cover:
        logging.warning("emitted without cover: %s", ", ".join(skipped_no_cover))
    if skipped_no_stl:
        logging.warning("skipped (no STL): %s", ", ".join(skipped_no_stl))
    if crashed:
        logging.warning("crashed: %s", ", ".join(crashed))
    logging.info("wrote manifest with %d models to %s", written, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
