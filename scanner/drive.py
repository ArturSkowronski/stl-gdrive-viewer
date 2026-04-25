"""Thin wrapper over Google Drive API v3.

Authenticates as the user via OAuth refresh token (so the scanner sees exactly
the same files the user does — including shortcuts to NomNom-owned folders).
Service accounts cannot see those, which is why we don't use them.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import Iterator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

LIST_FIELDS = (
    "nextPageToken,"
    "files(id,name,mimeType,size,imageMediaMetadata(width,height),"
    "shortcutDetails,webViewLink,modifiedTime)"
)


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: Optional[int]
    width: Optional[int]
    height: Optional[int]
    web_view_link: Optional[str]
    modified_time: Optional[str]
    shortcut_target_id: Optional[str]
    shortcut_target_mime: Optional[str]

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME or (
            self.mime_type == SHORTCUT_MIME
            and self.shortcut_target_mime == FOLDER_MIME
        )

    @property
    def effective_id(self) -> str:
        """For shortcuts, returns target id; otherwise own id."""
        if self.mime_type == SHORTCUT_MIME and self.shortcut_target_id:
            return self.shortcut_target_id
        return self.id

    @property
    def effective_mime(self) -> str:
        if self.mime_type == SHORTCUT_MIME and self.shortcut_target_mime:
            return self.shortcut_target_mime
        return self.mime_type

    @classmethod
    def from_api(cls, raw: dict) -> "DriveFile":
        sd = raw.get("shortcutDetails") or {}
        img = raw.get("imageMediaMetadata") or {}
        size_str = raw.get("size")
        return cls(
            id=raw["id"],
            name=raw["name"],
            mime_type=raw["mimeType"],
            size=int(size_str) if size_str is not None else None,
            width=img.get("width"),
            height=img.get("height"),
            web_view_link=raw.get("webViewLink"),
            modified_time=raw.get("modifiedTime"),
            shortcut_target_id=sd.get("targetId"),
            shortcut_target_mime=sd.get("targetMimeType"),
        )


def build_credentials_from_env() -> Credentials:
    client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]
    refresh_token = os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"]
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


class DriveClient:
    def __init__(self, credentials: Optional[Credentials] = None):
        creds = credentials or build_credentials_from_env()
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def list_children(self, folder_id: str) -> Iterator[DriveFile]:
        page_token: Optional[str] = None
        while True:
            resp = (
                self._service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields=LIST_FIELDS,
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for raw in resp.get("files", []):
                yield DriveFile.from_api(raw)
            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    def download_bytes(self, file_id: str, max_bytes: int = 8 * 1024 * 1024) -> bytes:
        """Download a file. Aborts past max_bytes to keep memory bounded."""
        request = self._service.files().get_media(
            fileId=file_id, supportsAllDrives=True
        )
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request, chunksize=1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
            if buf.tell() > max_bytes:
                raise ValueError(
                    f"file {file_id} exceeded {max_bytes} bytes — aborting download"
                )
        return buf.getvalue()
