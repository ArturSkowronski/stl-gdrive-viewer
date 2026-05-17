"""Telegram bot that uploads forwarded model files to Google Drive.

Pattern: the user forwards a NomNom-style message into a private chat.
The bot:

  1. Buffers messages of the same media_group_id for ~2s (Telegram
     delivers album messages as separate updates). Photo + archive in
     the same album → cover is picked from the album.
  2. Single archive message (no album) → download starts immediately.
     Photo-only messages from the same user are stored as a pending
     cover (TTL 2 min). After the archive download completes, the bot
     checks whether a cover arrived in the meantime and uses it.
  3. Archives (.zip/.7z/.rar) are extracted locally first; individual
     STL files land on Drive preserving sub-folder structure so the
     scanner walker classifies them normally.
  4. Cover saved as "Beauty shot.jpg" so the scanner hard-picks it.
  5. Uploads to `<DRIVE_ROOT_FOLDER_ID>/<cleaned-model-name>/` with
     resumable chunked upload. Next daily refresh picks it up.
  6. Replies ✅ + Drive folder URL, or ❌ + error.

Idempotent: if the target folder already contains files, re-forwarding
replies "already there" without re-uploading.

ACL: ALLOWED_USER_IDS gates who can trigger uploads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time as _time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Optional

from telegram import Message, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from drive_writer import folder_exists_nonempty, upload_dir_tree, upload_model_files

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

MODEL_EXTS = (".stl", ".7z", ".zip", ".rar", ".ctb", ".goo")
ARCHIVE_EXTS = (".7z", ".zip", ".rar")
MEDIA_GROUP_FLUSH_S = 2.0
COVER_TTL_S = 120.0  # how long a pending cover stays valid
WORK_DIR = Path(os.environ.get("BOT_WORK_DIR", "/tmp/stl-bot"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

_TRAILING_HANDLE_RE = re.compile(r"\s*@\w+\s*$")
_BRIDGE_RE = re.compile(r"_+\.\.\._+|_+-+_+|_{2,}")


def _clean_name(filename: str) -> str:
    base = filename.rsplit(".", 1)[0]
    base = _TRAILING_HANDLE_RE.sub("", base)
    base = _BRIDGE_RE.sub(" - ", base)
    base = base.replace("_", " ").strip(" -")
    stripped = re.sub(r"^[A-Z][a-zA-Z]+\s+[A-Z][a-zA-Z]+\s*-\s*", "", base)
    if len(stripped) >= 3:
        base = stripped
    return base.strip() or filename


def _allowed_user_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_USER_IDS", "")
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def _extract(archive_path: Path, dest: Path) -> None:
    """Extract archive into dest. Raises on failure."""
    dest.mkdir(parents=True, exist_ok=True)
    ext = archive_path.suffix.lower()
    if ext == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(dest)
    elif ext == ".7z":
        import py7zr  # soft dep; present in Docker image
        with py7zr.SevenZipFile(archive_path, mode="r") as zf:
            zf.extractall(path=dest)
    elif ext == ".rar":
        result = subprocess.run(
            ["7z", "x", str(archive_path), f"-o{dest}", "-y"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
    else:
        raise ValueError(f"unsupported archive: {ext}")


def _unwrap_single_dir(path: Path) -> Path:
    """If path contains exactly one subdirectory and no files, unwrap it.

    NomNom archives often look like Model.rar → Model/ → *.stl — we
    want the inner folder contents to land at Drive folder root.
    """
    children = list(path.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return path


# --- pending-cover store (for single-message uploads) --------------------

# (chat_id, user_id) → (photo_message, expiry_monotonic)
_pending_covers: dict[tuple, tuple[Message, float]] = {}
# Signalled when a cover arrives, so _process_single can wake up fast.
_cover_events: dict[tuple, asyncio.Event] = {}


def _store_pending_cover(chat_id: int, user_id: int, msg: Message) -> None:
    key = (chat_id, user_id)
    _pending_covers[key] = (msg, _time.monotonic() + COVER_TTL_S)
    ev = _cover_events.get(key)
    if ev is not None:
        ev.set()
    log.debug("stored pending cover from user %s in chat %s", user_id, chat_id)


def _pop_pending_cover(chat_id: int, user_id: int) -> Optional[Message]:
    key = (chat_id, user_id)
    entry = _pending_covers.pop(key, None)
    if entry and _time.monotonic() < entry[1]:
        return entry[0]
    return None


# --- media-group buffering -----------------------------------------------

_pending_groups: dict[tuple, list[Message]] = defaultdict(list)
_pending_group_tasks: dict[tuple, asyncio.Task] = {}


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or update.effective_user is None:
        return
    user_id = update.effective_user.id

    allow = _allowed_user_ids()
    if allow and user_id not in allow:
        log.warning("ignoring message from unauthorised user %s", user_id)
        return

    if msg.media_group_id:
        key = (msg.chat_id, msg.media_group_id)
        _pending_groups[key].append(msg)
        if key in _pending_group_tasks:
            _pending_group_tasks[key].cancel()
        _pending_group_tasks[key] = asyncio.create_task(
            _flush_group(key, context)
        )
    else:
        has_model_doc = msg.document and (
            (msg.document.file_name or "").lower().endswith(MODEL_EXTS)
        )
        if has_model_doc:
            # Start download immediately; cover check happens after download.
            asyncio.create_task(_process_single(msg, user_id, context))
        elif msg.photo and not msg.document:
            # Photo-only → store as pending cover for an in-flight upload.
            _store_pending_cover(msg.chat_id, user_id, msg)
        else:
            # Text or unsupported attachment — ignore silently.
            pass


async def _flush_group(key: tuple, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.sleep(MEDIA_GROUP_FLUSH_S)
    except asyncio.CancelledError:
        return
    messages = _pending_groups.pop(key, [])
    _pending_group_tasks.pop(key, None)
    if messages:
        await _process_batch(messages, context, user_id=None)


# --- core upload logic ---------------------------------------------------

async def _process_single(
    msg: Message, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Entry point for single-message (non-album) uploads.

    Runs the upload, then after the archive download is done checks
    whether a cover photo arrived in the meantime.
    """
    cover_key = (msg.chat_id, user_id)
    ev = asyncio.Event()
    _cover_events[cover_key] = ev
    try:
        await _process_batch([msg], context, user_id=user_id)
    finally:
        _cover_events.pop(cover_key, None)


async def _process_batch(
    messages: list[Message],
    context: ContextTypes.DEFAULT_TYPE,
    user_id: Optional[int],
) -> None:
    doc_msg: Optional[Message] = None
    photo_msg: Optional[Message] = None
    for m in messages:
        if doc_msg is None and m.document:
            fn = (m.document.file_name or "").lower()
            if fn.endswith(MODEL_EXTS):
                doc_msg = m
        if photo_msg is None and m.photo:
            photo_msg = m

    reply_to = messages[0]
    if doc_msg is None:
        await reply_to.reply_text(
            "Brak pliku modelu w forwardzie. Akceptuję wiadomości "
            "z załącznikiem .rar / .zip / .7z / .stl / .ctb / .goo."
        )
        return

    doc = doc_msg.document
    filename = doc.file_name or f"forwarded-{doc.file_id}.rar"
    size_mb = (doc.file_size or 0) / 1_000_000
    display_name = _clean_name(filename)

    progress = await reply_to.reply_text(
        f"📥 {display_name}\n"
        f"plik: `{filename}` ({size_mb:.1f} MB)\n"
        f"pobieram…",
        parse_mode="Markdown",
    )

    drive_root = os.environ["DRIVE_ROOT_FOLDER_ID"]
    try:
        existing_url = await asyncio.to_thread(
            folder_exists_nonempty, drive_root, display_name
        )
    except Exception as e:
        log.exception("drive existence check failed")
        await progress.edit_text(f"❌ Błąd sprawdzenia Drive: {e}")
        return
    if existing_url:
        await progress.edit_text(
            f"ℹ️ {display_name} już jest na Drive.\n{existing_url}"
        )
        return

    job_dir = WORK_DIR / f"job-{doc_msg.message_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    local_doc = job_dir / filename
    local_cover: Optional[Path] = None

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=str(local_doc))

        # After download: if the batch had no photo, check for a cover
        # the user sent while the download was running.
        if photo_msg is None and user_id is not None:
            photo_msg = _pop_pending_cover(doc_msg.chat_id, user_id)
            if photo_msg:
                log.info("using pending cover for %s", display_name)

        if photo_msg and photo_msg.photo:
            largest = photo_msg.photo[-1]
            cover_file = await context.bot.get_file(largest.file_id)
            local_cover = job_dir / "Beauty shot.jpg"
            await cover_file.download_to_drive(custom_path=str(local_cover))

        # Extract archives so individual STL files land on Drive.
        upload_dir: Optional[Path] = None
        is_archive = any(filename.lower().endswith(ext) for ext in ARCHIVE_EXTS)
        if is_archive:
            await progress.edit_text(
                f"📦 {display_name}\n"
                f"plik: `{filename}` ({size_mb:.1f} MB)\n"
                f"rozpakowuję…",
                parse_mode="Markdown",
            )
            extract_dir = job_dir / "extracted"
            try:
                await asyncio.to_thread(_extract, local_doc, extract_dir)
                upload_dir = _unwrap_single_dir(extract_dir)
                log.info("extracted %s → %s", filename, upload_dir)
            except Exception as e:
                log.warning("extraction failed (%s), uploading archive as-is", e)

        await progress.edit_text(
            f"☁️ {display_name}\n"
            f"plik: `{filename}` ({size_mb:.1f} MB)\n"
            f"wrzucam na Drive…",
            parse_mode="Markdown",
        )

        if upload_dir is not None:
            folder_url = await asyncio.to_thread(
                upload_dir_tree, drive_root, display_name, upload_dir, local_cover
            )
        else:
            folder_url = await asyncio.to_thread(
                upload_model_files, drive_root, display_name, local_doc, local_cover
            )

        await progress.edit_text(
            f"✅ {display_name}\n"
            f"plik: `{filename}` ({size_mb:.1f} MB)\n"
            f"{folder_url}",
            parse_mode="Markdown",
        )
        log.info("uploaded %s to %s", display_name, folder_url)

    except Exception as e:
        log.exception("upload failed for %s", filename)
        try:
            await progress.edit_text(f"❌ {display_name}: {e}")
        except Exception:
            pass
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def build_app() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    base_url = os.environ.get("TELEGRAM_BOT_API_URL", "").rstrip("/")
    builder = ApplicationBuilder().token(token)
    if base_url:
        builder = builder.base_url(f"{base_url}/bot").base_file_url(
            f"{base_url}/file/bot"
        )
        builder = builder.local_mode(True)
    builder = builder.request(
        HTTPXRequest(connect_timeout=30, read_timeout=300, write_timeout=300)
    )
    app = builder.build()
    app.add_handler(MessageHandler(filters.ALL, handle))
    return app


def main() -> None:
    app = build_app()
    log.info(
        "bot starting — work dir=%s, allow=%s", WORK_DIR, _allowed_user_ids() or "*"
    )
    app.run_polling(allowed_updates=["message", "edited_message", "channel_post"])


if __name__ == "__main__":
    main()
