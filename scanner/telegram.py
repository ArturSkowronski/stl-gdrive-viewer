"""Telegram channel scraper — index public channels as a second source.

Reads only the first page of `t.me/s/<channel>` (Telegram's public web
preview) on every run. Pagination through deep history is deliberately
NOT supported: the same incremental contract that protects Drive
("once indexed, doesn't change") applies here, and going further back
would multiply HTTP requests + scraping fragility for no real benefit.

A model = one media group from the channel:
  - 1..N consecutive image messages (the cover candidates)
  - a document message (the .rar/.zip/.7z) posted together with them

Output: list of TelegramModel objects with stable `id` of the form
`tg:<channel>:<message_id>` so scan.py's incremental dedup logic
works without any source-specific casing.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

PUBLIC_BASE = "https://t.me/s/{channel}"
MESSAGE_LINK = "https://t.me/{channel}/{message_id}"
FETCH_TIMEOUT_S = 20
USER_AGENT = (
    "Mozilla/5.0 (compatible; stl-gdrive-viewer/1.0; "
    "+https://github.com/ArturSkowronski/stl-gdrive-viewer)"
)

# "1.4 GB" / "160.4 MB" / "743 KB" — Telegram's widget formats sizes with a
# single decimal place and SI prefixes. Convert to bytes (approximate) so
# the manifest size field matches Drive's behaviour.
_SIZE_RE = re.compile(
    r"([\d,.]+)\s*(B|KB|MB|GB|TB)", re.IGNORECASE
)
_SIZE_UNITS = {"B": 1, "KB": 1_000, "MB": 1_000_000, "GB": 1_000_000_000,
               "TB": 1_000_000_000_000}

# Known model-file extensions inside Telegram-attached documents. We
# don't surface arbitrary attachments (PDFs, screenshots, etc.) — only
# the archives a user would actually download to print from.
_MODEL_FILE_EXTS = (".stl", ".7z", ".zip", ".rar", ".ctb", ".goo")


@dataclass
class TelegramFile:
    url: str               # https://t.me/<channel>/<msg_id>
    name: str              # original filename ("Mithril Helmet @Print3DWorld.zip")
    size: Optional[int]    # bytes, approximate (parsed from "160.4 MB")


@dataclass
class TelegramModel:
    channel: str           # "Best_STL_3D"
    message_id: int        # primary file message id (anchor of the group)
    display_name: str      # cleaned filename, used as card title
    cover_url: Optional[str] = None  # CDN URL of the first photo, if any
    text: str = ""         # caption / message text, used as a search hint
    files: List[TelegramFile] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"tg:{self.channel}:{self.message_id}"

    @property
    def message_url(self) -> str:
        return MESSAGE_LINK.format(channel=self.channel, message_id=self.message_id)


def _parse_size(text: str) -> Optional[int]:
    if not text:
        return None
    m = _SIZE_RE.search(text)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    return int(n * _SIZE_UNITS.get(unit, 1))


# Filenames in NomNom-style channels often look like
# "Bastet_Figures_..._MOXOMOR.rar" or "Mithril Helmet @Print3DWorld.zip".
# Strip extension, the @handle suffix, leading author prefix, and the
# bridge ellipsis — leaving a human label like "MOXOMOR" / "Mithril Helmet".
_TRAILING_HANDLE_RE = re.compile(r"\s*@\w+\s*$")
_AUTHOR_PREFIX_RE = re.compile(r"^[A-Za-z][\w]+_+", re.IGNORECASE)
_BRIDGE_RE = re.compile(r"_+\.\.\._+|_+-+_+|_{2,}")


def _clean_name(filename: str) -> str:
    base = filename.rsplit(".", 1)[0]
    base = _TRAILING_HANDLE_RE.sub("", base)
    base = _BRIDGE_RE.sub(" - ", base)
    base = base.replace("_", " ")
    # Drop the "Bastet Figures" author prefix when present so the
    # remaining tail (usually the actual character name) shows on the
    # card. Keep the original if stripping would leave too little.
    stripped = re.sub(r"^[A-Z][a-zA-Z]+\s+[A-Z][a-zA-Z]+\s*-\s*", "", base)
    if len(stripped) >= 3:
        base = stripped
    return base.strip(" -") or filename


def fetch_first_page(channel: str, opener=None) -> str:
    """Fetch the HTML of t.me/s/<channel>. Caller is expected to parse
    it via `parse_page`. Pulled out as its own helper so tests can feed
    a snapshot HTML straight to the parser without hitting the network.
    """
    url = PUBLIC_BASE.format(channel=urllib.parse.quote(channel))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    op = opener or urllib.request.urlopen
    with op(req, timeout=FETCH_TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_page(html: str, channel: str) -> List[TelegramModel]:
    """Parse the Telegram widget HTML into TelegramModel objects.

    Each `tgme_widget_message` block carries `data-post="<channel>/<id>"`
    and may include zero or more photos and zero or more documents.
    Album messages (Telegram media groups) appear as a single block in
    the widget with multiple `tgme_widget_message_photo_wrap` children
    when they're image-only, OR as a sequence of single-photo messages
    when there are documents mixed in. We treat each document-bearing
    message as one model, attaching the previous photo-only message
    (if any) as the cover.
    """
    from bs4 import BeautifulSoup  # deferred import — module imports fine without bs4

    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select("div.tgme_widget_message")

    # Build a flat ordered list of "raw posts": for each widget block,
    # extract message_id, photo URLs, documents, and caption.
    @dataclass
    class _Raw:
        message_id: int
        photos: List[str]
        docs: List[Tuple[str, Optional[int]]]  # (filename, size_bytes_or_none)
        text: str

    raw: List[_Raw] = []
    for b in blocks:
        # Skip service messages (channel-create, pin notices, etc.)
        if "service_message" in (b.get("class") or []):
            continue
        post = b.get("data-post") or ""
        if "/" not in post:
            continue
        try:
            mid = int(post.split("/", 1)[1])
        except ValueError:
            continue

        # Photos — both single-photo (`tgme_widget_message_photo_wrap`)
        # and grid-album variants render the background-image inline,
        # which is the only place we can recover the CDN URL from.
        photos: List[str] = []
        for p in b.select("a.tgme_widget_message_photo_wrap, a.tgme_widget_message_photo"):
            style = p.get("style") or ""
            m = re.search(r"background-image:\s*url\(['\"]?([^'\")]+)['\"]?\)", style)
            if m:
                photos.append(m.group(1))

        docs: List[Tuple[str, Optional[int]]] = []
        for d in b.select("a.tgme_widget_message_document"):
            title_el = d.select_one(".tgme_widget_message_document_title")
            extra_el = d.select_one(".tgme_widget_message_document_extra")
            name = (title_el.get_text(strip=True) if title_el else "") or ""
            size_text = (extra_el.get_text(strip=True) if extra_el else "") or ""
            if not name:
                continue
            if not name.lower().endswith(_MODEL_FILE_EXTS):
                continue
            docs.append((name, _parse_size(size_text)))

        text_el = b.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""

        raw.append(_Raw(message_id=mid, photos=photos, docs=docs, text=text))

    # Walk in order and pair: each document-bearing message becomes a
    # model. Its cover comes from photos in the same message OR — when
    # the file was posted as a follow-up to a media-group image — from
    # the most recent preceding photo-only message within ~3 message
    # ids (Telegram's media-group spans are usually 2-10 messages).
    models: List[TelegramModel] = []
    last_photo_msg: Optional[_Raw] = None
    for r in raw:
        if r.docs:
            cover = r.photos[0] if r.photos else None
            if cover is None and last_photo_msg is not None and (
                r.message_id - last_photo_msg.message_id <= 3
            ):
                cover = last_photo_msg.photos[0] if last_photo_msg.photos else None
            files = [
                TelegramFile(
                    url=MESSAGE_LINK.format(channel=channel, message_id=r.message_id),
                    name=name,
                    size=size,
                )
                for (name, size) in r.docs
            ]
            primary = files[0]
            models.append(
                TelegramModel(
                    channel=channel,
                    message_id=r.message_id,
                    display_name=_clean_name(primary.name),
                    cover_url=cover,
                    text=r.text or (last_photo_msg.text if last_photo_msg else ""),
                    files=files,
                )
            )
            last_photo_msg = None  # consumed
        elif r.photos:
            last_photo_msg = r

    return models


def fetch_channel(channel: str, known_ids: Optional[set] = None) -> List[TelegramModel]:
    """Scrape first page, return models not in `known_ids`.

    Caller (scan.py) is expected to feed `known_ids` from the cached
    manifest so we never reprocess a message we've already indexed.
    All deeper history is left implicit — once we've seen a message,
    it stays in the manifest until the orphan-prune step drops it
    because it disappeared from the channel.
    """
    try:
        html = fetch_first_page(channel)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        log.warning("telegram: fetch failed for %s: %s", channel, e)
        return []
    try:
        models = parse_page(html, channel)
    except Exception as e:
        log.warning("telegram: parse failed for %s: %s", channel, e)
        return []
    if known_ids is None:
        log.info("telegram: %d model(s) on first page (no known set)", len(models))
        return models
    fresh = [m for m in models if m.id not in known_ids]
    log.info(
        "telegram: %d total on first page, %d new",
        len(models), len(fresh),
    )
    return fresh


def fetch_thumbnail(url: str, timeout: int = FETCH_TIMEOUT_S) -> Optional[bytes]:
    """Pull the JPEG bytes of a Telegram widget cover image. Same
    CDN pattern as Drive's thumbnailLink fast path — no auth, no quota."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        log.warning("telegram: thumbnail fetch failed for %s: %s", url, e)
        return None
