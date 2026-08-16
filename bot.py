#!/usr/bin/env python3
"""
Telegram Video Compressor Bot («Жмыхач»)

Мониторит выбранные чаты, автоматически сжимает видео через ffmpeg
и отвечает уменьшенной версией. Также поддерживает ручное сжатие
командой /compress (ответом на сообщение с видео).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Set, Tuple

from dotenv import load_dotenv
from telegram import Update, Message
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required")

MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "600"))
CRF = int(os.getenv("CRF", "28"))
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "720"))
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "128k")
PRESET = os.getenv("PRESET", "medium")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
TMP_DIR = Path(os.getenv("TMP_DIR", "/app/tmp"))
MONITORED_FILE = DATA_DIR / "monitored_chats.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("video-compressor-bot")

monitored_chats: Set[int] = set()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_monitored() -> None:
    global monitored_chats
    if MONITORED_FILE.exists():
        try:
            data = json.loads(MONITORED_FILE.read_text(encoding="utf-8"))
            monitored_chats = set(int(x) for x in data)
            logger.info("Loaded %d monitored chat(s)", len(monitored_chats))
        except Exception as e:
            logger.error("Failed to load monitored chats: %s", e)
            monitored_chats = set()
    else:
        monitored_chats = set()


def save_monitored() -> None:
    try:
        MONITORED_FILE.write_text(
            json.dumps(sorted(monitored_chats), indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("Failed to save monitored chats: %s", e)


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def get_video_duration(path: Path) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception as e:
        logger.warning("ffprobe failed: %s", e)
    return None


def compress_video(
    input_path: Path,
    output_path: Path,
    progress_callback=None,
) -> bool:
    vf = f"scale=-2:'min({MAX_HEIGHT},ih)'"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-crf", str(CRF),
        "-preset", PRESET,
        "-vf", vf,
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        str(output_path),
    ]

    logger.info("Running ffmpeg: %s", " ".join(cmd))

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        duration = get_video_duration(input_path) or 0.0
        last_percent = -1.0
        start_time = time.time()

        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            line = line.strip()
            if not line:
                continue

            if line.startswith("out_time_ms="):
                try:
                    out_ms = int(line.split("=", 1)[1])
                    if duration > 0:
                        percent = min(99.0, (out_ms / 1_000_000) / duration * 100)
                        if progress_callback and abs(percent - last_percent) >= 2.0:
                            last_percent = percent
                            eta = None
                            if percent > 1:
                                elapsed = time.time() - start_time
                                eta = (elapsed / (percent / 100)) - elapsed
                            progress_callback(percent, eta)
                except ValueError:
                    pass
            elif line.startswith("progress=") and line.endswith("end"):
                break

        process.wait(timeout=3600)

        if process.returncode != 0:
            stderr = process.stderr.read() if process.stderr else ""
            logger.error("ffmpeg failed (code %s): %s", process.returncode, stderr[-2000:])
            return False

        if progress_callback:
            progress_callback(100.0, 0)
        return output_path.exists() and output_path.stat().st_size > 0

    except Exception as e:
        logger.exception("Compression error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Shared video processing
# ---------------------------------------------------------------------------

def _extract_video_info(message: Message) -> Optional[Tuple[str, int, str, int]]:
    """
    Extract (file_id, duration, file_name, file_size) from a message.
    Returns None if the message has no video.
    """
    if message.animation:
        return None
    if message.video:
        v = message.video
        return (
            v.file_id,
            v.duration or 0,
            getattr(v, "file_name", None) or "video.mp4",
            v.file_size or 0,
        )
    if (
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("video/")
        and not (message.document.file_name or "").lower().endswith(".gif")
        and message.document.mime_type != "image/gif"
    ):
        d = message.document
        return (
            d.file_id,
            0,
            d.file_name or "video.mp4",
            d.file_size or 0,
        )
    return None


async def process_video(
    target_message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_to: Optional[Message] = None,
) -> None:
    """
    Download → compress → upload pipeline.
    target_message  – message that contains the video
    reply_to        – message to reply to (defaults to target_message)
    """
    if reply_to is None:
        reply_to = target_message

    chat_id = target_message.chat.id
    info = _extract_video_info(target_message)
    if not info:
        return

    file_id, duration, file_name, file_size = info

    if duration and duration > MAX_DURATION_SECONDS:
        logger.info(
            "Skipping video in chat %s: duration %ss > limit %ss",
            chat_id, duration, MAX_DURATION_SECONDS,
        )
        return

    status_msg: Optional[Message] = None
    work_dir: Optional[Path] = None

    try:
        status_msg = await reply_to.reply_text(
            "⏳ Обработка видео — начинаю сжатие…\n"
            f"Оригинал: {_human_size(file_size)}"
        )
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        work_dir = Path(tempfile.mkdtemp(prefix="vcomp_", dir=str(TMP_DIR)))
        input_path = work_dir / "input"
        output_path = work_dir / "output.mp4"

        await status_msg.edit_text("⬇️ Скачиваю видео…")
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=str(input_path))

        if not input_path.exists() or input_path.stat().st_size == 0:
            await status_msg.edit_text("❌ Не удалось скачать видео.")
            return

        if not duration:
            duration = get_video_duration(input_path) or 0
            if duration > MAX_DURATION_SECONDS:
                await status_msg.edit_text(
                    f"⏭ Пропущено — длительность {duration:.0f} с "
                    f"превышает лимит {MAX_DURATION_SECONDS} с."
                )
                return

        original_size = input_path.stat().st_size

        last_edit = 0.0
        loop = asyncio.get_running_loop()

        def progress_cb(percent: float, eta: Optional[float]):
            nonlocal last_edit
            now = time.time()
            if now - last_edit < 3.5 and percent < 99:
                return
            last_edit = now
            eta_str = f"~{int(eta)} с осталось" if eta and eta > 0 else "…"
            text = (
                f"🔄 Сжимаю… *{percent:.0f}%*\n"
                f"{eta_str}\n"
                f"Оригинал: {_human_size(original_size)}"
            )

            def _schedule(t=text):
                asyncio.create_task(_safe_edit(status_msg, t))

            loop.call_soon_threadsafe(_schedule)

        await status_msg.edit_text(
            f"🔄 Сжимаю…\nОригинал: {_human_size(original_size)}\n"
            f"Настройки: {MAX_HEIGHT}p / CRF {CRF} / {PRESET}"
        )

        success = await asyncio.to_thread(
            compress_video,
            input_path,
            output_path,
            progress_cb,
        )

        if not success or not output_path.exists():
            await status_msg.edit_text("❌ Сжатие не удалось. Проверьте логи.")
            return

        new_size = output_path.stat().st_size
        ratio = (1 - new_size / original_size) * 100 if original_size else 0

        await status_msg.edit_text(
            f"⬆️ Загружаю сжатое видео…\n"
            f"{_human_size(original_size)} → {_human_size(new_size)} "
            f"(на {ratio:.0f}% меньше)"
        )
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        caption = (
            f"📦 Сжато\n"
            f"{_human_size(original_size)} → {_human_size(new_size)} "
            f"({ratio:.0f}% меньше)"
        )

        with output_path.open("rb") as f:
            await reply_to.reply_video(
                video=f,
                caption=caption,
                supports_streaming=True,
                filename=Path(file_name).stem + "_compressed.mp4",
            )

        try:
            await status_msg.delete()
        except TelegramError:
            await status_msg.edit_text("✅ Готово.")

        logger.info(
            "Compressed video in chat %s: %s → %s (%.1f%%)",
            chat_id, _human_size(original_size), _human_size(new_size), ratio,
        )

    except Exception as e:
        logger.exception("Error handling video in chat %s: %s", chat_id, e)
        if status_msg:
            try:
                await status_msg.edit_text(f"❌ Ошибка: {e}")
            except TelegramError:
                pass
    finally:
        if work_dir and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


async def _safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        pass


def _human_size(num: int | float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ТБ"


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🎥 *Жмыхач*\n\n"
        "Я автоматически сжимаю видео в отслеживаемых чатах "
        "и отправляю уменьшенные версии.\n\n"
        "*Команды:*\n"
        "/add — Добавить *этот чат* в отслеживаемые\n"
        "/remove — Убрать *этот чат* из отслеживаемых\n"
        "/compress — Сжать видео (ответьте этой командой на сообщение с видео)\n"
        "/status — Показать настройки и лимиты\n"
        "/help — Это сообщение\n\n"
        "⚠️ Если бот не видит обычные сообщения — отключите Privacy Mode "
        "в @BotFather (`/setprivacy` → Disable). "
        "В этом случае можно пользоваться командой /compress."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def add_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return

    chat_id = chat.id
    if chat_id in monitored_chats:
        await update.message.reply_text("✅ Этот чат уже отслеживается.")
        return

    monitored_chats.add(chat_id)
    save_monitored()
    title = chat.title or chat.full_name or str(chat_id)
    await update.message.reply_text(
        f"✅ Добавлен *{title}* (`{chat_id}`) в отслеживаемые.\n"
        f"Я буду сжимать новые видео, которые появятся здесь.",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("Added chat %s (%s)", chat_id, title)


async def remove_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return

    chat_id = chat.id
    if chat_id not in monitored_chats:
        await update.message.reply_text("ℹ️ Этот чат не отслеживается.")
        return

    monitored_chats.discard(chat_id)
    save_monitored()
    title = chat.title or chat.full_name or str(chat_id)
    await update.message.reply_text(
        f"🗑 Удалён *{title}* (`{chat_id}`) из отслеживаемых.",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("Removed chat %s", chat_id)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "⚙️ *Статус бота*\n\n"
        f"• Отслеживаемые чаты: *{len(monitored_chats)}*\n"
        f"• Макс. длительность: *{MAX_DURATION_SECONDS} с* "
        f"({MAX_DURATION_SECONDS // 60} мин)\n"
        f"• Макс. высота: *{MAX_HEIGHT}p*\n"
        f"• CRF (качество): *{CRF}* (выше = меньше файл)\n"
        f"• Пресет: *{PRESET}*\n"
        f"• Битрейт аудио: *{AUDIO_BITRATE}*\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def compress_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ручное сжатие: пользователь отвечает /compress на сообщение с видео.
    Работает даже если чат не в пуле отслеживания и даже при включённом Privacy Mode.
    """
    message = update.message
    if not message:
        return

    replied = message.reply_to_message
    if not replied:
        await message.reply_text(
            "Ответьте командой /compress на сообщение, которое содержит видео."
        )
        return

    # Ignore bot's own messages
    if replied.from_user and replied.from_user.is_bot:
        await message.reply_text("Не могу сжимать сообщения ботов.")
        return

    if not _extract_video_info(replied):
        await message.reply_text(
            "В сообщении, на которое вы ответили, нет видео."
        )
        return

    # Process the replied message; reply status/result to the /compress command
    await process_video(replied, context, reply_to=message)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Автоматическая обработка видео в отслеживаемых чатах."""
    message = update.message
    if not message or not message.chat:
        return

    if message.from_user and message.from_user.is_bot:
        return

    if message.chat.id not in monitored_chats:
        return

    if not _extract_video_info(message):
        return

    await process_video(message, context)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_monitored()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("add", add_chat))
    application.add_handler(CommandHandler("remove", remove_chat))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("compress", compress_cmd))

    application.add_handler(
        MessageHandler(
            (filters.VIDEO | filters.Document.VIDEO) & ~filters.COMMAND,
            handle_video,
        )
    )

    logger.info(
        "Bot starting… max_duration=%ss, max_height=%s, crf=%s",
        MAX_DURATION_SECONDS, MAX_HEIGHT, CRF,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
