from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("video-compressor-bot")


@dataclass(frozen=True)
class AppConfig:
    bot_token: str
    max_duration_seconds: int
    crf: int
    max_height: int
    audio_bitrate: str
    preset: str
    default_fps: int
    compress_concurrency: int
    data_dir: Path
    tmp_dir: Path
    monitored_file: Path
    chat_settings_file: Path


def load_config() -> AppConfig:
    load_dotenv()
    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token:
        raise SystemExit("BOT_TOKEN environment variable is required")

    data_dir = Path(os.getenv("DATA_DIR", "/app/data"))
    tmp_dir = Path(os.getenv("TMP_DIR", "/app/tmp"))
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        bot_token=bot_token,
        max_duration_seconds=int(os.getenv("MAX_DURATION_SECONDS", "600")),
        crf=int(os.getenv("CRF", "28")),
        max_height=int(os.getenv("MAX_HEIGHT", "720")),
        audio_bitrate=os.getenv("AUDIO_BITRATE", "128k"),
        preset=os.getenv("PRESET", "medium"),
        default_fps=int(os.getenv("DEFAULT_FPS", "30")),
        compress_concurrency=max(1, int(os.getenv("COMPRESS_CONCURRENCY", "1"))),
        data_dir=data_dir,
        tmp_dir=tmp_dir,
        monitored_file=data_dir / "monitored_chats.json",
        chat_settings_file=data_dir / "chat_settings.json",
    )
