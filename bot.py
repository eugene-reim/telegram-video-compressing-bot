#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional
from telegram import ForceReply, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from config import load_config
from media import (
    VOICE_MAX_BYTES,
    compress_video,
    extract_audio_mp3,
    extract_audio_opus,
    extract_video_info,
    get_video_duration,
    has_audio_stream,
    human_size,
)
from models import CompressionSettings
from settings_ui import (
    parse_setting,
    setting_label,
    setting_options_keyboard,
    settings_keyboard,
    settings_text,
)
from status import StatusEditor, safe_edit
from storage import (
    load_chat_settings as load_chat_settings_file,
    load_monitored as load_monitored_file,
    save_chat_settings as save_chat_settings_file,
    save_monitored as save_monitored_file,
)

request = HTTPXRequest(
    connection_pool_size=8,
    connect_timeout=30.0,
    read_timeout=30.0,
    write_timeout=60.0,
    pool_timeout=30.0,
)
config = load_config()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("video-compressor-bot")
monitored_chats: set[int] = set()
chat_settings: dict[int, CompressionSettings] = {}
compress_semaphore = asyncio.Semaphore(config.compress_concurrency)
default_settings = CompressionSettings(
    max_duration_seconds=config.max_duration_seconds,
    max_height=config.max_height,
    crf=config.crf,
    preset=config.preset,
    audio_bitrate=config.audio_bitrate,
)


def get_chat_settings(chat_id: int) -> CompressionSettings:
    return chat_settings.get(chat_id, default_settings)


def load_monitored() -> None:
    global monitored_chats
    monitored_chats = load_monitored_file(config.monitored_file)


def save_monitored() -> None:
    save_monitored_file(config.monitored_file, monitored_chats)


def load_chat_settings() -> None:
    global chat_settings
    chat_settings = load_chat_settings_file(config.chat_settings_file)


def save_chat_settings() -> None:
    save_chat_settings_file(config.chat_settings_file, chat_settings)


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
    info = extract_video_info(target_message)
    if not info:
        return

    file_id = info.file_id
    duration = info.duration
    file_name = info.file_name
    if duration and duration > settings.max_duration_seconds:
        logger.info(
            "Skipping video in chat %s: duration %ss > limit %ss",
            chat_id,
            duration,
            settings.max_duration_seconds,
        )
        return

    status = StatusEditor()
    work_dir: Optional[Path] = None

    queued = compress_semaphore.locked()
    if queued:
        try:
            status.attach(await reply_to.reply_text("⏳ Видео в очереди на сжатие…"))
        except TelegramError as e:
            logger.warning("Failed to send queue notice in chat %s: %s", chat_id, e)

    async with compress_semaphore:
        try:
            start_text = "⏳ Обработка видео - начинаю сжатие…"
            if status.message is None:
                status.attach(await reply_to.reply_text(start_text))
            else:
                await status.set_now(start_text)

            await context.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO
            )

            work_dir = Path(tempfile.mkdtemp(prefix="vcomp_", dir=str(config.tmp_dir)))
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
                        f"⏭ Пропущено — длительность {duration:.0f}с"
                        f"превышает лимит {settings.max_duration_seconds}с."
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
                eta_str = f"~{int(eta)}с" if eta and eta > 0 else "..."
                text = f"🔄 Сжимаю… *{percent:.0f}%* {eta_str}"
                loop.call_soon_threadsafe(lambda t=text: status.set(t, progress=True))

            await status.set_now(f"🔄 Сжимаю...")
            status.enable_progress()

            success = await asyncio.to_thread(
                compress_video, input_path, output_path, settings, progress_cb, fps
            )

            status.disable_progress()
            await status.wait()

            if not success or not output_path.exists():
                await status.set_now("❌ Сжатие не удалось. Проверьте логи.")
                return

            new_size = output_path.stat().st_size
            ratio = (1 - new_size / original_size) * 100 if original_size else 0

            if ratio <= 0:
                await status.set_now("ℹ️ Сжатие не уменьшило размер файла.")
                status.close()
                return

            await status.set_now(
                f"⬆️ Загружаю сжатое видео… {human_size(original_size)} > "
                f"{human_size(new_size)} (на {ratio:.0f}% меньше)"
            )
            await context.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO
            )

            caption = (
                f"📦 Сжато {human_size(original_size)} > {human_size(new_size)} "
                f"(на {ratio:.0f}% меньше)"
            )
            with output_path.open("rb") as f:
                await reply_to.reply_video(
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                    filename=Path(file_name).stem + "_compressed.mp4",
                )

            status.close()
            if status.message is not None:
                try:
                    await status.message.delete()
                except TelegramError:
                    await safe_edit(status.message, "✅ Готово.")

            logger.info(
                "Compressed video in chat %s: %s > %s (%.1f%%)",
                chat_id,
                human_size(original_size),
                human_size(new_size),
                ratio,
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
    info = extract_video_info(target_message)
    if not info:
        return

    file_id = info.file_id
    duration = info.duration
    file_name = info.file_name
    if duration and duration > config.max_duration_seconds:
        await reply_to.reply_text(
            f"⏭ Слишком длинное видео ({duration} с), лимит {config.max_duration_seconds} с."
        )
        return

    status = StatusEditor()
    work_dir: Optional[Path] = None

    queued = compress_semaphore.locked()
    if queued:
        try:
            status.attach(
                await reply_to.reply_text("⏳ В очереди на извлечение аудио…")
            )
        except TelegramError as e:
            logger.warning("Failed to send queue notice in chat %s: %s", chat_id, e)

    async with compress_semaphore:
        try:
            start_text = "⏳ Извлекаю аудио…"
            if status.message is None:
                status.attach(await reply_to.reply_text(start_text))
            else:
                await status.set_now(start_text)

            work_dir = Path(tempfile.mkdtemp(prefix="aext_", dir=str(config.tmp_dir)))
            input_path = work_dir / "input"
            opus_path = work_dir / "audio.ogg"
            mp3_path = work_dir / "audio.mp3"

            await status.set_now("⬇️ Скачиваю видео…")
            await context.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.UPLOAD_VOICE
            )
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=str(input_path))

            if not input_path.exists() or input_path.stat().st_size == 0:
                await status.set_now("❌ Не удалось скачать видео.")
                return

            if not duration:
                duration = get_video_duration(input_path) or 0
                if duration > config.max_duration_seconds:
                    await status.set_now(
                        f"⏭ Пропущено — длительность {duration:.0f} с "
                        f"превышает лимит {config.max_duration_seconds} с."
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
            success = await asyncio.to_thread(
                extract_audio_opus, input_path, opus_path, progress_cb
            )
            status.disable_progress()
            await status.wait()

            if not success:
                await status.set_now("❌ Не удалось извлечь аудио. Проверьте логи.")
                return

            sent = False
            opus_size = opus_path.stat().st_size
            stem = Path(file_name).stem

            if opus_size <= VOICE_MAX_BYTES:
                await status.set_now(f"⬆️ Отправляю голосовое… {human_size(opus_size)}")
                await context.bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.UPLOAD_VOICE
                )
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
                    await status.set_now(
                        "ℹ️ Голосовое не отправилось, пробую аудиофайл…"
                    )

            if not sent:
                await status.set_now("🔄 Кодирую MP3…")
                status.enable_progress()
                mp3_ok = await asyncio.to_thread(
                    extract_audio_mp3,
                    input_path,
                    mp3_path,
                    config.audio_bitrate,
                    progress_cb,
                )
                status.disable_progress()
                await status.wait()
                if not mp3_ok:
                    await status.set_now("❌ Не удалось подготовить аудиофайл.")
                    return
                await status.set_now(
                    f"⬆️ Отправляю аудиофайл… {human_size(mp3_path.stat().st_size)}"
                )
                await context.bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.UPLOAD_VOICE
                )
                with mp3_path.open("rb") as f:
                    await reply_to.reply_audio(
                        audio=f,
                        duration=int(duration) if duration else None,
                        filename=stem + ".mp3",
                        title=stem,
                    )

            status.close()
            if status.message is not None:
                try:
                    await status.message.delete()
                except TelegramError:
                    await safe_edit(status.message, "✅ Готово.")

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
        f"• Одновременных сжатий: *{config.compress_concurrency}*\n"
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
            "✅ Настройки для этого чата были сброшены.\n\n"
            + settings_text(default_settings),
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(),
        )
        return
    if len(context.args) != 2:
        await message.reply_text(
            "Использование: /settings поле значение или /settings reset"
        )
        return
    try:
        updated = parse_setting(
            context.args[0].lower(), context.args[1].lower(), get_chat_settings(chat_id)
        )
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
            "✅ Настройки для этого чата были сброшены.\n\n"
            + settings_text(default_settings),
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(),
        )
        return
    if data == "settings:close":
        user_data.pop("pending_setting", None)
        await query.answer()
        try:
            await query.edit_message_text(
                "✅ Настройки были сохранены.", reply_markup=None
            )
        except TelegramError:
            await query.delete_message()
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
            f"Введите {setting_label(name)} в ответ на это сообщение.",
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


async def settings_value_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    user_data = context.user_data
    name = user_data.pop("pending_setting", None) if user_data is not None else None
    if not message or not name or not message.reply_to_message:
        return
    if (
        not message.reply_to_message.from_user
        or not message.reply_to_message.from_user.is_bot
    ):
        return
    if not message.text:
        return
    try:
        updated = parse_setting(
            name, message.text.strip().lower(), get_chat_settings(message.chat.id)
        )
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
        await message.reply_text(
            "Ответьте командой /compress на сообщение, которое содержит видео."
        )
        return
    if replied.from_user and replied.from_user.is_bot:
        await message.reply_text("Не могу сжимать сообщения ботов.")
        return
    if not extract_video_info(replied):
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
    if not extract_video_info(replied):
        await message.reply_text("В сообщении, на которое вы ответили, нет видео.")
        return
    settings = get_chat_settings(message.chat.id)
    fps = settings.fps or config.default_fps
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
    if not extract_video_info(replied):
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
    if not extract_video_info(message):
        return
    await process_video(message, context)


def main() -> None:
    load_monitored()
    load_chat_settings()
    application = (
        Application.builder()
        .token(config.bot_token)
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
    application.add_handler(
        CallbackQueryHandler(settings_callback, pattern=r"^settings:")
    )
    application.add_handler(CommandHandler("compress", compress_cmd))
    application.add_handler(CommandHandler("compress_fps", compress_fps_cmd))
    application.add_handler(CommandHandler("extract_audio", extract_audio_cmd))
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.REPLY & ~filters.COMMAND, settings_value_reply
        )
    )
    application.add_handler(
        MessageHandler(
            (filters.VIDEO | filters.Document.VIDEO) & ~filters.COMMAND, handle_video
        )
    )
    logger.info(
        "Bot starting… max_duration=%ss, max_height=%s, crf=%s, concurrency=%s",
        config.max_duration_seconds,
        config.max_height,
        config.crf,
        config.compress_concurrency,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
