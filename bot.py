#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Set, Tuple

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message, ForceReply
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

request = HTTPXRequest(
    connection_pool_size=8,
    connect_timeout=30.0,
    read_timeout=30.0,
    write_timeout=60.0,
    pool_timeout=30.0,
)


load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required")

MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "600"))
CRF = int(os.getenv("CRF", "28"))
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "720"))
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "128k")
PRESET = os.getenv("PRESET", "medium")
DEFAULT_FPS = int(os.getenv("DEFAULT_FPS", "30"))
COMPRESS_CONCURRENCY = max(1, int(os.getenv("COMPRESS_CONCURRENCY", "1")))

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
TMP_DIR = Path(os.getenv("TMP_DIR", "/app/tmp"))
MONITORED_FILE = DATA_DIR / "monitored_chats.json"
CHAT_SETTINGS_FILE = DATA_DIR / "chat_settings.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("video-compressor-bot")

monitored_chats: Set[int] = set()
chat_settings: dict[int, "CompressionSettings"] = {}
compress_semaphore = asyncio.Semaphore(COMPRESS_CONCURRENCY)


@dataclass(frozen=True)
class CompressionSettings:
    max_duration_seconds: int = MAX_DURATION_SECONDS
    max_height: int = MAX_HEIGHT
    crf: int = CRF
    preset: str = PRESET
    audio_bitrate: str = AUDIO_BITRATE
    fps: int = 0


def get_chat_settings(chat_id: int) -> CompressionSettings:
    return chat_settings.get(chat_id, CompressionSettings())


def settings_text(settings: CompressionSettings) -> str:
    fps = f"{settings.fps} fps" if settings.fps else "без ограничения FPS"
    return (
        "<b>⚙️ Настройки сжатия для этого чата</b>\n\n"
        f"• Макс. длительность: <b>{settings.max_duration_seconds} с</b>\n"
        f"• Макс. разрешение: <b>{settings.max_height}p</b>\n"
        f"• Качество CRF: <b>{settings.crf}</b>\n"
        f"• Пресет: <b>{settings.preset}</b>\n"
        f"• Аудио: <b>{settings.audio_bitrate}</b>\n"
        f"• FPS: <b>{fps}</b>\n\n"
        "Выберите параметр ниже. Для своего значения нажмите «Другое»."
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📐 Разрешение", callback_data="settings:height"), InlineKeyboardButton("🎚 CRF", callback_data="settings:crf")],
        [InlineKeyboardButton("🚀 Preset", callback_data="settings:preset"), InlineKeyboardButton("🔊 Аудио", callback_data="settings:audio")],
        [InlineKeyboardButton("⏱ Длительность", callback_data="settings:duration"), InlineKeyboardButton("🎞 FPS", callback_data="settings:fps")],
        [InlineKeyboardButton("♻️ Сбросить", callback_data="settings:reset")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="settings:close")],
    ])


def setting_options_keyboard(name: str) -> InlineKeyboardMarkup:
    options = {
        "height": [("360p", "360"), ("480p", "480"), ("720p", "720"), ("1080p", "1080"), ("Другое", "custom")],
        "crf": [("23", "23"), ("28", "28"), ("32", "32"), ("36", "36"), ("Другое", "custom")],
        "preset": [("fast", "fast"), ("medium", "medium"), ("slow", "slow"), ("veryslow", "veryslow")],
        "audio": [("64k", "64k"), ("96k", "96k"), ("128k", "128k"), ("192k", "192k"), ("Другое", "custom")],
        "duration": [("5 мин", "300"), ("10 мин", "600"), ("20 мин", "1200"), ("60 мин", "3600"), ("Другое", "custom")],
        "fps": [("Без лимита", "0"), ("24", "24"), ("30", "30"), ("60", "60"), ("Другое", "custom")],
    }
    buttons = [InlineKeyboardButton(label, callback_data=f"settings:set:{name}:{value}") for label, value in options[name]]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.extend([
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings:back")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="settings:close")],
    ])
    return InlineKeyboardMarkup(rows)


def setting_label(name: str) -> str:
    return {
        "height": "максимальное разрешение в пикселях",
        "crf": "CRF от 0 до 51",
        "audio": "битрейт аудио, например 96k",
        "duration": "максимальная длительность в секундах",
        "fps": "FPS от 0 до 60",
    }[name]


def parse_setting(name: str, value: str, current: CompressionSettings) -> CompressionSettings:
    values: dict[str, Any] = asdict(current)
    if name == "duration":
        parsed = int(value)
        if not 1 <= parsed <= 86400:
            raise ValueError("duration должен быть от 1 до 86400 секунд")
        values["max_duration_seconds"] = parsed
    elif name == "height":
        parsed = int(value)
        if not 144 <= parsed <= 4320:
            raise ValueError("height должен быть от 144 до 4320")
        values["max_height"] = parsed
    elif name == "crf":
        parsed = int(value)
        if not 0 <= parsed <= 51:
            raise ValueError("crf должен быть от 0 до 51")
        values["crf"] = parsed
    elif name == "preset":
        if value not in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}:
            raise ValueError("preset: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower или veryslow")
        values["preset"] = value
    elif name == "audio":
        if not re.fullmatch(r"(?:\d{1,3})k", value) or not 16 <= int(value[:-1]) <= 512:
            raise ValueError("audio должен быть в формате 16k-512k")
        values["audio_bitrate"] = value
    elif name == "fps":
        parsed = int(value)
        if not 0 <= parsed <= 60:
            raise ValueError("fps должен быть от 0 до 60; 0 отключает ограничение")
        values["fps"] = parsed
    else:
        raise ValueError("неизвестное поле")
    return CompressionSettings(**values)


def save_chat_settings() -> None:
    try:
        CHAT_SETTINGS_FILE.write_text(
            json.dumps({str(chat_id): asdict(settings) for chat_id, settings in chat_settings.items()}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("Failed to save chat settings: %s", e)


def load_chat_settings() -> None:
    global chat_settings
    if not CHAT_SETTINGS_FILE.exists():
        chat_settings = {}
        return
    try:
        data = json.loads(CHAT_SETTINGS_FILE.read_text(encoding="utf-8"))
        loaded: dict[int, CompressionSettings] = {}
        for chat_id, values in data.items():
            loaded[int(chat_id)] = CompressionSettings(**values)
        chat_settings = loaded
        logger.info("Loaded settings for %d chat(s)", len(chat_settings))
    except Exception as e:
        logger.error("Failed to load chat settings: %s", e)
        chat_settings = {}


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


def get_video_duration(path: Path) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception as e:
        logger.warning("ffprobe failed: %s", e)
    return None


def compress_video(
    input_path: Path,
    output_path: Path,
    settings: CompressionSettings,
    progress_callback=None,
    fps: int = 0,
) -> bool:
    filters = [f"scale=-2:'min({settings.max_height},ih)'"]
    if fps > 0:
        filters.append(f"fps={fps}")
    vf = ",".join(filters)
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-c:v", "libx264", "-crf", str(settings.crf), "-preset", settings.preset, "-vf", vf,
        "-c:a", "aac", "-b:a", settings.audio_bitrate, "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", str(output_path),
    ]
    logger.info("Running ffmpeg: %s", " ".join(cmd))
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        duration = get_video_duration(input_path) or 0.0
        last_percent = -1.0
        start_time = time.time()
        if process.stdout is None:
            raise RuntimeError("ffmpeg stdout is unavailable")
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


VOICE_MAX_BYTES = 50 * 1024 * 1024


def has_audio_stream(path: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception as e:
        logger.warning("ffprobe audio check failed: %s", e)
        return False


def _run_ffmpeg_with_progress(cmd: list[str], input_path: Path, output_path: Path, progress_callback=None) -> bool:
    logger.info("Running ffmpeg: %s", " ".join(cmd))
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        duration = get_video_duration(input_path) or 0.0
        last_percent = -1.0
        start_time = time.time()
        if process.stdout is None:
            raise RuntimeError("ffmpeg stdout is unavailable")
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
        logger.exception("ffmpeg error: %s", e)
        return False


def extract_audio_opus(input_path: Path, output_path: Path, progress_callback=None) -> bool:
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vn", "-map", "0:a:0",
        "-c:a", "libopus", "-b:a", "48k", "-ac", "1", "-application", "voip",
        "-progress", "pipe:1", "-nostats", str(output_path),
    ]
    return _run_ffmpeg_with_progress(cmd, input_path, output_path, progress_callback)


def extract_audio_mp3(input_path: Path, output_path: Path, progress_callback=None) -> bool:
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vn", "-map", "0:a:0",
        "-c:a", "libmp3lame", "-b:a", AUDIO_BITRATE,
        "-progress", "pipe:1", "-nostats", str(output_path),
    ]
    return _run_ffmpeg_with_progress(cmd, input_path, output_path, progress_callback)


class StatusEditor:
    """One in-flight edit_text per status message. Latest text wins."""

    def __init__(self, msg: Optional[Message] = None) -> None:
        self.msg = msg
        self._pending: Optional[str] = None
        self._shown: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self._allow_progress = False

    def attach(self, msg: Optional[Message]) -> None:
        self.msg = msg

    def set(self, text: str, *, progress: bool = False) -> None:
        if self._closed or self.msg is None:
            return
        if progress and not self._allow_progress:
            return
        if text == self._shown or text == self._pending:
            return
        self._pending = text
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush())

    def enable_progress(self) -> None:
        self._allow_progress = True

    def disable_progress(self) -> None:
        self._allow_progress = False

    async def set_now(self, text: str) -> None:
        self.set(text)
        await self.wait()

    async def wait(self) -> None:
        task = self._task
        if task is not None and not task.done():
            try:
                await task
            except Exception:
                pass

    def close(self) -> None:
        self._closed = True
        self._allow_progress = False
        self._pending = None

    async def _flush(self) -> None:
        while self._pending is not None and not self._closed and self.msg is not None:
            text = self._pending
            self._pending = None
            if text == self._shown:
                continue
            await _safe_edit(self.msg, text)
            self._shown = text


async def _safe_edit(msg: Optional[Message], text: str) -> None:
    if msg is None:
        return
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


def _extract_video_info(message: Message) -> Optional[Tuple[str, int, str, int]]:
    if message.animation:
        return None
    if message.video:
        v = message.video
        return (v.file_id, v.duration or 0, getattr(v, "file_name", None) or "video.mp4", v.file_size or 0)
    if (
        message.document and message.document.mime_type
        and message.document.mime_type.startswith("video/")
        and not (message.document.file_name or "").lower().endswith(".gif")
        and message.document.mime_type != "image/gif"
    ):
        d = message.document
        return (d.file_id, 0, d.file_name or "video.mp4", d.file_size or 0)
    return None


async def process_video(
    target_message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_to: Optional[Message] = None,
    fps: int = 0,
    settings: Optional[CompressionSettings] = None,
) -> None:
    if reply_to is None:
        reply_to = target_message

    chat_id = target_message.chat.id
    settings = settings or get_chat_settings(chat_id)
    info = _extract_video_info(target_message)
    if not info:
        return

    file_id, duration, file_name, file_size = info
    if duration and duration > settings.max_duration_seconds:
        logger.info("Skipping video in chat %s: duration %ss > limit %ss", chat_id, duration, settings.max_duration_seconds)
        return

    status = StatusEditor()
    work_dir: Optional[Path] = None

    queued = compress_semaphore.locked()
    if queued:
        try:
            status.attach(await reply_to.reply_text(
                "⏳ Видео в очереди на сжатие…"
            ))
        except TelegramError as e:
            logger.warning("Failed to send queue notice in chat %s: %s", chat_id, e)

    async with compress_semaphore:
        try:
            start_text = (
                "⏳ Обработка видео - начинаю сжатие…"
            )
            if status.msg is None:
                status.attach(await reply_to.reply_text(start_text))
            else:
                await status.set_now(start_text)

            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

            work_dir = Path(tempfile.mkdtemp(prefix="vcomp_", dir=str(TMP_DIR)))
            input_path = work_dir / "input"
            output_path = work_dir / "output.mp4"

            await status.set_now("⬇️ Скачиваю видео…")
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=str(input_path))

            if not input_path.exists() or input_path.stat().st_size == 0:
                await status.set_now("❌ Не удалось скачать видео.")
                return

            if not duration:
                duration = get_video_duration(input_path) or 0
                if duration > settings.max_duration_seconds:
                    await status.set_now(
                        f"⏭ Пропущено — длительность {duration:.0f} с "
                        f"превышает лимит {settings.max_duration_seconds} с."
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
                    f"🔄 Сжимаю… *{percent:.0f}%* {eta_str}"
                )
                loop.call_soon_threadsafe(lambda t=text: status.set(t, progress=True))

            fps_info = f" / {fps} fps" if fps > 0 else ""
            await status.set_now(
                f"🔄 Сжимаю…\nОригинал: {_human_size(original_size)}\n"
                f"Настройки: {settings.max_height}p / CRF {settings.crf} / {settings.preset}"
                f" / {settings.audio_bitrate}{fps_info}"
            )
            status.enable_progress()

            success = await asyncio.to_thread(compress_video, input_path, output_path, settings, progress_cb, fps)

            status.disable_progress()
            await status.wait()

            if not success or not output_path.exists():
                await status.set_now("❌ Сжатие не удалось. Проверьте логи.")
                return

            new_size = output_path.stat().st_size
            ratio = (1 - new_size / original_size) * 100 if original_size else 0
            
            if ratio <=0:
                await status.set_now("ℹ️ Сжатие не уменьшило размер файла.")
                status.close()
                return
            
            await status.set_now(
                f"⬆️ Загружаю сжатое видео… {_human_size(original_size)} > "
                f"{_human_size(new_size)} (на {ratio:.0f}% меньше)"
            )
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

            caption = (
                f"📦 Сжато {_human_size(original_size)} > {_human_size(new_size)} "
                f"(на {ratio:.0f}% меньше)"
            )
            with output_path.open("rb") as f:
                await reply_to.reply_video(
                    video=f, caption=caption, supports_streaming=True,
                    filename=Path(file_name).stem + "_compressed.mp4",
                )

            status.close()
            if status.msg is not None:
                try:
                    await status.msg.delete()
                except TelegramError:
                    await _safe_edit(status.msg, "✅ Готово.")

            logger.info(
                "Compressed video in chat %s: %s > %s (%.1f%%)",
                chat_id, _human_size(original_size), _human_size(new_size), ratio,
            )
        except Exception as e:
            logger.exception("Error handling video in chat %s: %s", chat_id, e)
            await status.set_now(f"❌ Ошибка: {e}")
        finally:
            status.close()
            if work_dir and work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)


async def process_extract_audio(
    target_message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_to: Optional[Message] = None,
) -> None:
    if reply_to is None:
        reply_to = target_message

    chat_id = target_message.chat.id
    info = _extract_video_info(target_message)
    if not info:
        return

    file_id, duration, file_name, file_size = info
    if duration and duration > MAX_DURATION_SECONDS:
        await reply_to.reply_text(
            f"⏭ Слишком длинное видео ({duration} с), лимит {MAX_DURATION_SECONDS} с."
        )
        return

    status = StatusEditor()
    work_dir: Optional[Path] = None

    queued = compress_semaphore.locked()
    if queued:
        try:
            status.attach(await reply_to.reply_text("⏳ В очереди на извлечение аудио…"))
        except TelegramError as e:
            logger.warning("Failed to send queue notice in chat %s: %s", chat_id, e)

    async with compress_semaphore:
        try:
            start_text = "⏳ Извлекаю аудио…"
            if status.msg is None:
                status.attach(await reply_to.reply_text(start_text))
            else:
                await status.set_now(start_text)

            work_dir = Path(tempfile.mkdtemp(prefix="aext_", dir=str(TMP_DIR)))
            input_path = work_dir / "input"
            opus_path = work_dir / "audio.ogg"
            mp3_path = work_dir / "audio.mp3"

            await status.set_now("⬇️ Скачиваю видео…")
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=str(input_path))

            if not input_path.exists() or input_path.stat().st_size == 0:
                await status.set_now("❌ Не удалось скачать видео.")
                return

            if not duration:
                duration = get_video_duration(input_path) or 0
                if duration > MAX_DURATION_SECONDS:
                    await status.set_now(
                        f"⏭ Пропущено — длительность {duration:.0f} с "
                        f"превышает лимит {MAX_DURATION_SECONDS} с."
                    )
                    return

            if not has_audio_stream(input_path):
                await status.set_now("❌ В этом видео нет аудиодорожки.")
                return

            last_edit = 0.0
            loop = asyncio.get_running_loop()

            def progress_cb(percent: float, eta: Optional[float]):
                nonlocal last_edit
                now = time.time()
                if now - last_edit < 3.5 and percent < 99:
                    return
                last_edit = now
                eta_str = f"~{int(eta)} с осталось" if eta and eta > 0 else "…"
                text = f"🔄 Извлекаю аудио… *{percent:.0f}%* {eta_str}"
                loop.call_soon_threadsafe(lambda t=text: status.set(t, progress=True))

            await status.set_now("🔄 Кодирую в голосовое (OGG/Opus)…")
            status.enable_progress()
            success = await asyncio.to_thread(extract_audio_opus, input_path, opus_path, progress_cb)
            status.disable_progress()
            await status.wait()

            if not success:
                await status.set_now("❌ Не удалось извлечь аудио. Проверьте логи.")
                return

            sent = False
            opus_size = opus_path.stat().st_size
            stem = Path(file_name).stem

            if opus_size <= VOICE_MAX_BYTES:
                await status.set_now(f"⬆️ Отправляю голосовое… {_human_size(opus_size)}")
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                try:
                    with opus_path.open("rb") as f:
                        await reply_to.reply_voice(
                            voice=f,
                            duration=int(duration) if duration else None,
                            filename=stem + ".ogg",
                        )
                    sent = True
                except TelegramError as e:
                    logger.warning("reply_voice failed in chat %s: %s", chat_id, e)
                    await status.set_now("ℹ️ Голосовое не отправилось, пробую аудиофайл…")

            if not sent:
                await status.set_now("🔄 Кодирую MP3…")
                status.enable_progress()
                mp3_ok = await asyncio.to_thread(extract_audio_mp3, input_path, mp3_path, progress_cb)
                status.disable_progress()
                await status.wait()
                if not mp3_ok:
                    await status.set_now("❌ Не удалось подготовить аудиофайл.")
                    return
                await status.set_now(f"⬆️ Отправляю аудиофайл… {_human_size(mp3_path.stat().st_size)}")
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                with mp3_path.open("rb") as f:
                    await reply_to.reply_audio(
                        audio=f,
                        duration=int(duration) if duration else None,
                        filename=stem + ".mp3",
                        title=stem,
                    )

            status.close()
            if status.msg is not None:
                try:
                    await status.msg.delete()
                except TelegramError:
                    await _safe_edit(status.msg, "✅ Готово.")

            logger.info("Extracted audio in chat %s from %s", chat_id, file_name)
        except Exception as e:
            logger.exception("Error extracting audio in chat %s: %s", chat_id, e)
            await status.set_now(f"❌ Ошибка: {e}")
        finally:
            status.close()
            if work_dir and work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    text = (
        "🎥 *Жмыхач*\n\n"
        "Я автоматически сжимаю видео в отслеживаемых чатах "
        "и отправляю уменьшенные версии.\n\n"
        "*Команды:*\n"
        "/add — Добавить *этот чат* в отслеживаемые\n"
        "/remove — Убрать *этот чат* из отслеживаемых\n"
        "/compress — Сжать видео (ответьте этой командой на сообщение с видео)\n"
        "/extract_audio — Извлечь аудио из видео (ответьте командой на сообщение с видео)\n"
        "/status — Показать настройки и лимиты\n"
        "/settings — Посмотреть или изменить настройки этого чата\n"
        "/help — Это сообщение\n\n"
        "⚠️ Если бот не видит обычные сообщения — отключите Privacy Mode "
        "в @BotFather (`/setprivacy` → Disable). "
        "Альтернативно можно пользоваться командой /compress."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def add_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.message
    if not chat or not message:
        return
    chat_id = chat.id
    if chat_id in monitored_chats:
        await message.reply_text("✅ Этот чат уже отслеживается.")
        return
    monitored_chats.add(chat_id)
    save_monitored()
    title = chat.title or chat.full_name or str(chat_id)
    await message.reply_text(
        f"✅ Чат *{title}* добавлен в отслеживаемые.\n"
        f"Я буду автоматически сжимать все новые видео!",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("Added chat %s (%s)", chat_id, title)


async def remove_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.message
    if not chat or not message:
        return
    chat_id = chat.id
    if chat_id not in monitored_chats:
        await message.reply_text("ℹ️ Этот чат не отслеживается.")
        return
    monitored_chats.discard(chat_id)
    save_monitored()
    title = chat.title or chat.full_name or str(chat_id)
    await message.reply_text(
        f"🗑 Удалён *{title}* (`{chat_id}`) из отслеживаемых.",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("Removed chat %s", chat_id)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    settings = get_chat_settings(message.chat.id)
    text = (
        "⚙️ *Статус бота*\n\n"
        f"• Отслеживаемые чаты: *{len(monitored_chats)}*\n"
        f"- Настройки этого чата:\n"
        f"• Макс. длительность: *{settings.max_duration_seconds} с* "
        f"({settings.max_duration_seconds // 60} мин)\n"
        f"• Макс. разрешение: *{settings.max_height}p*\n"
        f"• CRF (качество): *{settings.crf}* (выше = меньше файл)\n"
        f"• Пресет: *{settings.preset}*\n"
        f"• Битрейт аудио: *{settings.audio_bitrate}*\n"
        f"• FPS: *{settings.fps or 'без ограничения'}*\n"
        f"• Одновременных сжатий: *{COMPRESS_CONCURRENCY}*\n"
    )
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.chat:
        return
    chat_id = message.chat.id
    if not context.args:
        await message.reply_text(
            settings_text(get_chat_settings(chat_id)),
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(),
        )
        return
    if context.args[0].lower() == "reset":
        chat_settings.pop(chat_id, None)
        save_chat_settings()
        await message.reply_text(
            "✅ Настройки для этого чата были сброшены.\n\n" + settings_text(CompressionSettings()),
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(),
        )
        return
    if len(context.args) != 2:
        await message.reply_text("Использование: /settings поле значение или /settings reset")
        return
    try:
        updated = parse_setting(context.args[0].lower(), context.args[1].lower(), get_chat_settings(chat_id))
    except (TypeError, ValueError) as e:
        await message.reply_text(
            f"❌ {e}\n\n{settings_text(get_chat_settings(chat_id))}",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(),
        )
        return
    chat_settings[chat_id] = updated
    save_chat_settings()
    await message.reply_text(
        "✅ Настройки для этого чата были сохранены.\n\n" + settings_text(updated),
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(),
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not isinstance(query.message, Message):
        return
    callback_message = query.message
    user_data = context.user_data
    if user_data is None:
        return
    chat_id = callback_message.chat.id
    data = query.data or ""
    if data == "settings:back":
        await query.answer()
        await query.edit_message_text(
            settings_text(get_chat_settings(chat_id)),
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(),
        )
        return
    if data == "settings:reset":
        await query.answer()
        chat_settings.pop(chat_id, None)
        save_chat_settings()
        await query.edit_message_text(
            "✅ Настройки для этого чата были сброшены.\n\n" + settings_text(CompressionSettings()),
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(),
        )
        return
    if data == "settings:close":
        user_data.pop("pending_setting", None)
        await query.answer()
        try:
            await callback_message.delete()
        except TelegramError:
            await query.edit_message_text("Настройки закрыты.", reply_markup=None)
        return
    parts = data.split(":")
    if len(parts) == 2 and parts[0] == "settings":
        name = parts[1]
        if name in {"height", "crf", "audio", "duration", "fps", "preset"}:
            await query.answer()
            await query.edit_message_text(
                f"<b>Изменение параметра: {name}</b>\n\nВыберите значение:",
                parse_mode=ParseMode.HTML,
                reply_markup=setting_options_keyboard(name),
            )
        return
    if len(parts) != 4 or parts[:2] != ["settings", "set"]:
        return
    name, value = parts[2:]
    if value == "custom":
        await query.answer()
        user_data["pending_setting"] = name
        await callback_message.reply_text(
            f"Введите {setting_label(name)} в ответ на это сообщение:",
            reply_markup=ForceReply(selective=True),
        )
        return
    try:
        updated = parse_setting(name, value, get_chat_settings(chat_id))
    except (TypeError, ValueError) as e:
        await query.answer(str(e), show_alert=True)
        return
    chat_settings[chat_id] = updated
    save_chat_settings()
    await query.answer()
    await query.edit_message_text(
        "✅ Настройки для этого чата были сохранены.\n\n" + settings_text(updated),
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(),
    )


async def settings_value_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user_data = context.user_data
    name = user_data.pop("pending_setting", None) if user_data is not None else None
    if not message or not name or not message.reply_to_message:
        return
    if not message.reply_to_message.from_user or not message.reply_to_message.from_user.is_bot:
        return
    if not message.text:
        return
    try:
        updated = parse_setting(name, message.text.strip().lower(), get_chat_settings(message.chat.id))
    except (TypeError, ValueError) as e:
        await message.reply_text(f"❌ {e}. Нажмите /settings и попробуйте снова.")
        return
    chat_settings[message.chat.id] = updated
    save_chat_settings()
    await message.reply_text(
        "✅ Настройки для этого чата были сохранены.\n\n" + settings_text(updated),
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(),
    )


async def compress_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    replied = message.reply_to_message
    if not replied:
        await message.reply_text("Ответьте командой /compress на сообщение, которое содержит видео.")
        return
    if replied.from_user and replied.from_user.is_bot:
        await message.reply_text("Не могу сжимать сообщения ботов.")
        return
    if not _extract_video_info(replied):
        await message.reply_text("В сообщении, на которое вы ответили, нет видео.")
        return
    await process_video(replied, context, reply_to=message)


async def compress_fps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    replied = message.reply_to_message
    if not replied:
        await message.reply_text(
            "Ответьте командой /compress_fps на сообщение с видео.\n"
            "Можно указать FPS: /compress_fps 24"
        )
        return
    if not _extract_video_info(replied):
        await message.reply_text("В сообщении, на которое вы ответили, нет видео.")
        return
    settings = get_chat_settings(message.chat.id)
    fps = settings.fps or DEFAULT_FPS
    if context.args:
        try:
            fps = int(context.args[0])
            if fps < 1 or fps > 60:
                raise ValueError
        except ValueError:
            await message.reply_text("FPS должен быть числом от 1 до 60.")
            return
    await process_video(replied, context, reply_to=message, fps=fps, settings=settings)


async def extract_audio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    replied = message.reply_to_message
    if not replied:
        await message.reply_text(
            "Ответьте командой /extract_audio на сообщение с видео."
        )
        return
    if not _extract_video_info(replied):
        await message.reply_text("В сообщении, на которое вы ответили, нет видео.")
        return
    await process_extract_audio(replied, context, reply_to=message)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


def main() -> None:
    load_monitored()
    load_chat_settings()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=3, group_max_rate=15))
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("add", add_chat))
    application.add_handler(CommandHandler("remove", remove_chat))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("settings", settings_cmd))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^settings:"))
    application.add_handler(CommandHandler("compress", compress_cmd))
    application.add_handler(CommandHandler("compress_fps", compress_fps_cmd))
    application.add_handler(CommandHandler("extract_audio", extract_audio_cmd))
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, settings_value_reply))
    application.add_handler(MessageHandler((filters.VIDEO | filters.Document.VIDEO) & ~filters.COMMAND, handle_video))
    logger.info(
        "Bot starting… max_duration=%ss, max_height=%s, crf=%s, concurrency=%s",
        MAX_DURATION_SECONDS, MAX_HEIGHT, CRF, COMPRESS_CONCURRENCY,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
