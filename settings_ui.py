from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from models import CompressionSettings

PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}


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
        [InlineKeyboardButton("🚀 Пресет", callback_data="settings:preset"), InlineKeyboardButton("🔊 Аудио", callback_data="settings:audio")],
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
        if value not in PRESETS:
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
