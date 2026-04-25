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
    from scanner.selector import pick_cover, pick_stl  # noqa: E402
    from scanner.thumbs import thumb_path, write_thumb  # noqa: E402
    from scanner.walker import walk  # noqa: E402
else:
    from .drive import DriveClient
    from .selector import pick_cover, pick_stl
    from .thumbs import thumb_path, write_thumb
    from .walker import walk


def _stl_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


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

    if args.limit:
        models = models[: args.limit]

    manifest_models = []
    for m in models:
        cover = pick_cover(client, m)
        stl = pick_stl(m)
        if not cover:
            logging.warning("skip %s — no usable cover image", m.name)
            continue
        if not stl:
            logging.warning("skip %s — no STL", m.name)
            continue

        thumb_dest = thumb_path(thumbs_dir, m.name, cover.file.id)
        write_thumb(cover.pil_image, thumb_dest)
        thumb_rel = thumb_dest.relative_to(out_path.parent).as_posix()

        manifest_models.append(
            {
                "id": m.folder_id,
                "name": m.name,
                "release": m.release,
                "folder_url": m.web_view_link,
                "thumb": thumb_rel,
                "stl": {
                    "file_id": stl.file.id,
                    "name": stl.file.name,
                    "size": stl.file.size,
                    "view_url": stl.file.web_view_link or _stl_view_url(stl.file.id),
                },
                "stl_count": len(m.stl_candidates),
            }
        )

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
    logging.info("wrote manifest with %d models to %s", len(manifest_models), out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
