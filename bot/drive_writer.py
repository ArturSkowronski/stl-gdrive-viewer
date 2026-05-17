"""Google Drive write helper for the bot.

Distinct from `scanner/drive.py` because the scanner is strictly
read-only (the project's licence stance) and we don't want broader
scope bleeding into the daily scan. The bot uses a separate refresh
token minted with `--write` via `scanner/auth_bootstrap.py`.

Two operations:
  - file_exists_in_folder(root, folder_name, file_name) -> URL or None
  - upload_model_files(root, folder_name, doc_path, cover_path) -> URL
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

log = logging.getLogger("bot.drive")

WRITE_SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB per resumable chunk


def _service():
    """Build a Drive v3 service from OAuth refresh credentials. Refresh
    is performed eagerly — the resulting access token is reused for the
    full upload cycle (the underlying client transparently refreshes
    again if it expires mid-upload)."""
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        scopes=WRITE_SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _escape_q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _find_folder(svc, parent_id: str, name: str) -> Optional[str]:
    q = (
        f"name = '{_escape_q(name)}' and "
        f"'{parent_id}' in parents and "
        f"mimeType = '{FOLDER_MIME}' and "
        f"trashed = false"
    )
    resp = svc.files().list(
        q=q,
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _find_file(svc, parent_id: str, name: str) -> Optional[dict]:
    q = (
        f"name = '{_escape_q(name)}' and "
        f"'{parent_id}' in parents and "
        f"mimeType != '{FOLDER_MIME}' and "
        f"trashed = false"
    )
    resp = svc.files().list(
        q=q,
        fields="files(id,name,webViewLink)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0] if files else None


def _create_folder(svc, parent_id: str, name: str) -> str:
    body = {
        "name": name,
        "mimeType": FOLDER_MIME,
        "parents": [parent_id],
    }
    folder = svc.files().create(
        body=body,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return folder["id"]


_SKIP_NAMES = frozenset(["__MACOSX", ".DS_Store", "Thumbs.db"])


def _should_skip(name: str) -> bool:
    return name in _SKIP_NAMES or name.startswith("._")


def folder_exists_nonempty(drive_root: str, model_folder_name: str) -> Optional[str]:
    """Return folder URL if <drive_root>/<model_folder_name>/ exists and
    has at least one file, otherwise None.

    Used as idempotency guard — covers both the archive-upload case (old)
    and the extract-then-upload case (new) where there's no single
    canonical filename to check against.
    """
    svc = _service()
    folder_id = _find_folder(svc, drive_root, model_folder_name)
    if not folder_id:
        return None
    resp = svc.files().list(
        q=f"'{folder_id}' in parents and mimeType != '{FOLDER_MIME}' and trashed = false",
        fields="files(id)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    if resp.get("files"):
        return f"https://drive.google.com/drive/folders/{folder_id}"
    return None


def file_exists_in_folder(
    drive_root: str, model_folder_name: str, file_name: str
) -> Optional[str]:
    """If `<drive_root>/<model_folder_name>/<file_name>` already exists,
    return its webViewLink. Otherwise return None.

    Lets the bot skip downloading + re-uploading multi-GB archives when
    the user accidentally forwards the same post twice.
    """
    svc = _service()
    folder_id = _find_folder(svc, drive_root, model_folder_name)
    if not folder_id:
        return None
    existing = _find_file(svc, folder_id, file_name)
    if not existing:
        return None
    return existing.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder_id}"


def _upload(svc, parent_id: str, local_path: Path) -> dict:
    mime, _ = mimetypes.guess_type(str(local_path))
    media = MediaFileUpload(
        str(local_path),
        mimetype=mime,
        resumable=True,
        chunksize=UPLOAD_CHUNK_SIZE,
    )
    body = {"name": local_path.name, "parents": [parent_id]}
    request = svc.files().create(
        body=body,
        media_body=media,
        fields="id,webViewLink",
        supportsAllDrives=True,
    )
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            log.info("uploading %s — %d%%", local_path.name, pct)
    return response


def _upload_dir(svc, parent_id: str, local_dir: Path) -> None:
    """Recursively upload local_dir contents into parent_id on Drive.

    Skips macOS metadata artefacts (__MACOSX, .DS_Store, ._* files).
    Already-existing files are skipped (idempotent per filename).
    """
    for item in sorted(local_dir.iterdir()):
        if _should_skip(item.name):
            continue
        if item.is_dir():
            sub_id = _find_folder(svc, parent_id, item.name)
            if sub_id is None:
                sub_id = _create_folder(svc, parent_id, item.name)
            _upload_dir(svc, sub_id, item)
        elif item.is_file():
            if not _find_file(svc, parent_id, item.name):
                _upload(svc, parent_id, item)


def upload_dir_tree(
    drive_root: str,
    model_folder_name: str,
    local_dir: Path,
    cover_path: Optional[Path],
) -> str:
    """Upload a local directory tree under <drive_root>/<model_folder_name>/.

    Used when an archive was extracted locally — individual STL files
    land on Drive instead of the archive blob, preserving sub-folder
    structure (Presupported/, Unsupported/, etc.) for the scanner walker.
    """
    svc = _service()
    folder_id = _find_folder(svc, drive_root, model_folder_name)
    if folder_id is None:
        folder_id = _create_folder(svc, drive_root, model_folder_name)
        log.info("created folder %s under %s", model_folder_name, drive_root)
    else:
        log.info("reusing folder %s (%s)", model_folder_name, folder_id)

    _upload_dir(svc, folder_id, local_dir)

    if cover_path is not None and cover_path.exists():
        if not _find_file(svc, folder_id, cover_path.name):
            _upload(svc, folder_id, cover_path)

    return f"https://drive.google.com/drive/folders/{folder_id}"


def upload_model_files(
    drive_root: str,
    model_folder_name: str,
    doc_path: Path,
    cover_path: Optional[Path],
) -> str:
    """Create-or-find `<drive_root>/<model_folder_name>/`, upload the
    document (multi-GB ok thanks to resumable chunked upload), and
    optionally the cover image alongside it. Returns the folder URL —
    the user gets that pasted back in chat."""
    svc = _service()
    folder_id = _find_folder(svc, drive_root, model_folder_name)
    if folder_id is None:
        folder_id = _create_folder(svc, drive_root, model_folder_name)
        log.info("created folder %s under %s", model_folder_name, drive_root)
    else:
        log.info("reusing folder %s (%s)", model_folder_name, folder_id)

    _upload(svc, folder_id, doc_path)

    if cover_path is not None and cover_path.exists():
        # Only upload cover if there isn't one already — repeated
        # forwards of the same model shouldn't pollute the folder with
        # cover-1.jpg / cover-2.jpg / ... duplicates.
        if not _find_file(svc, folder_id, cover_path.name):
            _upload(svc, folder_id, cover_path)

    return f"https://drive.google.com/drive/folders/{folder_id}"
