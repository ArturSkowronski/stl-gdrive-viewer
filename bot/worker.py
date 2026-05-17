"""Telegram bot that uploads forwarded model files to Google Drive.

Pattern: the user forwards a NomNom-style media-group (album of cover
images followed by a .rar/.zip/.stl document) into a private chat with
this bot. The bot:

  1. Buffers messages of the same media_group_id for ~2s (Telegram
     delivers media-group messages as separate updates).
  2. Picks the first photo from the group as the cover, the first
     archive/STL document as the model file.
  3. Downloads both via the LOCAL Telegram Bot API server (which has
     no 20 MB limit, unlike the public api.telegram.org Bot API).
  4. Uploads to `<DRIVE_ROOT_FOLDER_ID>/<cleaned-model-name>/` with
     resumable chunked upload. Same folder structure the scanner
     already walks — once the upload finishes, the next daily refresh
     picks it up like any other Drive model.
  5. Replies in chat with ✅ + Drive folder URL, or ❌ + error.

Idempotent: re-forwarding the same archive replies "already there"
without re-uploading.

ACL: ALLOWED_USER_IDS (comma-separated Telegram user IDs) restricts
who can trigger uploads. Anything from other users is silently ignored.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
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

from drive_writer import file_exists_in_folder, upload_model_files

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

MODEL_EXTS = (".stl", ".7z", ".zip", ".rar", ".ctb", ".goo")
MEDIA_GROUP_FLUSH_S = 2.0
WORK_DIR = Path(os.environ.get("BOT_WORK_DIR", "/tmp/stl-bot"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Filenames in forwarded posts look like
# `Bastet_Figures_..._MOXOMOR.rar` or `Mithril Helmet @Print3DWorld.zip`.
# Strip the extension, the trailing `@handle`, glue underscores into
# spaces, drop the author-prefix part — leaving a tidy display label
# that the Drive walker turns into the card title.
_TRAILING_HANDLE_RE = re.compile(r"\s*@\w+\s*$")
_BRIDGE_RE = re.compile(r"_+\.\.\._+|_+-+_+|_{2,}")


def _clean_name(filename: str) -> str:
    base = filename.rsplit(".", 1)[0]
    base = _TRAILING_HANDLE_RE.sub("", base)
    base = _BRIDGE_RE.sub(" - ", base)
    base = base.replace("_", " ").strip(" -")
    # Drop "Bastet Figures - " kind of author prefix when present so
    # the remaining tail (character name) becomes the folder name.
    stripped = re.sub(r"^[A-Z][a-zA-Z]+\s+[A-Z][a-zA-Z]+\s*-\s*", "", base)
    if len(stripped) >= 3:
        base = stripped
    return base.strip() or filename


def _allowed_user_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_USER_IDS", "")
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


# Buffer for media groups in flight. Key: (chat_id, media_group_id).
_pending_groups: dict[tuple, list[Message]] = defaultdict(list)
_pending_tasks: dict[tuple, asyncio.Task] = {}


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
        # Reset the flush timer — wait until *no more messages arrive
        # for MEDIA_GROUP_FLUSH_S* before processing the group.
        if key in _pending_tasks:
            _pending_tasks[key].cancel()
        _pending_tasks[key] = asyncio.create_task(_flush_group(key, context))
    else:
        await _process_batch([msg], context)


async def _flush_group(key: tuple, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.sleep(MEDIA_GROUP_FLUSH_S)
    except asyncio.CancelledError:
        return
    messages = _pending_groups.pop(key, [])
    _pending_tasks.pop(key, None)
    if messages:
        await _process_batch(messages, context)


async def _process_batch(
    messages: list[Message], context: ContextTypes.DEFAULT_TYPE
) -> None:
    # Find the first document with a model-ish extension. Treat the
    # first photo (in any message of the batch) as the cover candidate.
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

    # Drive idempotency check — if the file already lives in the
    # target folder, skip the download entirely.
    drive_root = os.environ["DRIVE_ROOT_FOLDER_ID"]
    try:
        existing_url = await asyncio.to_thread(
            file_exists_in_folder, drive_root, display_name, filename
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
        # Telegram document download. Against the local Bot API server
        # this returns a file path on the shared volume rather than a
        # downloadable URL — but PTB's download_to_drive handles both
        # transparently.
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=str(local_doc))

        if photo_msg and photo_msg.photo:
            # Largest variant is last in the list.
            largest = photo_msg.photo[-1]
            cover_file = await context.bot.get_file(largest.file_id)
            local_cover = job_dir / "cover.jpg"
            await cover_file.download_to_drive(custom_path=str(local_cover))

        await progress.edit_text(
            f"☁️ {display_name}\n"
            f"plik: `{filename}` ({size_mb:.1f} MB)\n"
            f"wrzucam na Drive…",
            parse_mode="Markdown",
        )

        folder_url = await asyncio.to_thread(
            upload_model_files,
            drive_root,
            display_name,
            local_doc,
            local_cover,
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
        # Point PTB at the locally-hosted Bot API server. The /bot
        # suffix is added automatically by PTB.
        builder = builder.base_url(f"{base_url}/bot").base_file_url(
            f"{base_url}/file/bot"
        )
        # Local Bot API serves files via filesystem paths in the
        # response; we still need HTTPX for the JSON API itself.
        builder = builder.local_mode(True)
    builder = builder.request(
        HTTPXRequest(connect_timeout=30, read_timeout=300, write_timeout=300)
    )
    app = builder.build()
    app.add_handler(MessageHandler(filters.ALL, handle))
    return app


def main() -> None:
    app = build_app()
    log.info("bot starting — work dir=%s, allow=%s", WORK_DIR, _allowed_user_ids() or "*")
    app.run_polling(allowed_updates=["message", "edited_message", "channel_post"])


if __name__ == "__main__":
    main()
