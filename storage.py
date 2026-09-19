from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

from models import CompressionSettings

logger = logging.getLogger("video-compressor-bot")


def load_monitored(path: Path) -> set[int]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        monitored = {int(value) for value in data}
        logger.info("Loaded %d monitored chat(s)", len(monitored))
        return monitored
    except Exception as exc:
        logger.error("Failed to load monitored chats: %s", exc)
        return set()


def save_monitored(path: Path, monitored_chats: set[int]) -> None:
    try:
        path.write_text(json.dumps(sorted(monitored_chats), indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to save monitored chats: %s", exc)


def load_chat_settings(path: Path) -> dict[int, CompressionSettings]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        settings = {
            int(chat_id): CompressionSettings(**values)
            for chat_id, values in data.items()
        }
        logger.info("Loaded settings for %d chat(s)", len(settings))
        return settings
    except Exception as exc:
        logger.error("Failed to load chat settings: %s", exc)
        return {}


def save_chat_settings(path: Path, settings: Mapping[int, CompressionSettings]) -> None:
    try:
        path.write_text(
            json.dumps({str(chat_id): asdict(value) for chat_id, value in settings.items()}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("Failed to save chat settings: %s", exc)
