from __future__ import annotations

import asyncio
from typing import Optional

from telegram import Message
from telegram.constants import ParseMode
from telegram.error import TelegramError


async def safe_edit(message: Optional[Message], text: str) -> None:
    if message is None:
        return
    try:
        await message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        pass


class StatusEditor:
    """Serialize status edits so the latest text wins."""

    def __init__(self, message: Optional[Message] = None) -> None:
        self.message = message
        self._pending: Optional[str] = None
        self._shown: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self._allow_progress = False

    def attach(self, message: Optional[Message]) -> None:
        self.message = message

    def set(self, text: str, *, progress: bool = False) -> None:
        if self._closed or self.message is None:
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
        while self._pending is not None and not self._closed and self.message is not None:
            text = self._pending
            self._pending = None
            if text == self._shown:
                continue
            await safe_edit(self.message, text)
            self._shown = text
