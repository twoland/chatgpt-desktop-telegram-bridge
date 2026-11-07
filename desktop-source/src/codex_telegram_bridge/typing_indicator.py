"""Best-effort Telegram activity status for confirmed, currently active turns."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .desktop_client import DesktopClientError
from .telegram_api import TelegramApiError

logger = logging.getLogger(__name__)
TYPING_REFRESH_SECONDS = 4.0
TYPING_POLL_SECONDS = 1.0


class TypingIndicatorMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._typing_retry = {}  # chat_id -> (next allowed monotonic time, failures)

    async def run(self):
        task = asyncio.create_task(self._typing_loop(), name="telegram-typing")
        self._tasks.add(task)
        try:
            await super().run()
        finally:
            # Also covers failures before BridgeApp enters its shutdown handler.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._tasks.discard(task)

    async def _typing_loop(self):
        while not self._shutdown.is_set():
            try:
                await self._sync_typing_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Typing indicator update failed")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=TYPING_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _sync_typing_once(self):
        if self._shutdown.is_set() or self._bot_id is None:
            return
        active = {}
        for thread_id, thread in list(self.state.threads.items()):
            chat_id = self._chat_for_thread(thread)
            if chat_id is None:
                continue
            try:
                conversation = await self.desktop.read_thread(thread_id)
            except (DesktopClientError, OSError, ValueError):
                continue  # A stale status must not keep the indicator alive.
            turn = conversation.latest_turn if conversation else None
            if (turn and turn.status == "inProgress" and not conversation.requests
                    and self._is_telegram_turn(thread, turn.turn_id)):
                active.setdefault(chat_id, []).append(thread)
        # Each request has its own short deadline; one slow chat cannot stall another.
        await asyncio.gather(*(self._send_typing(chat_id, threads) for chat_id, threads in active.items()))

    async def _send_typing(self, chat_id, threads):
        if self._shutdown.is_set() or not any(
            self.state.threads.get(thread.thread_id) is thread
            and self._chat_for_thread(thread) == chat_id for thread in threads
        ):
            return  # Recheck after reads in case the task was detached or reassigned.
        now = time.monotonic()
        next_at, failures = self._typing_retry.get(chat_id, (0, 0))
        if now < next_at:
            return
        self._typing_retry[chat_id] = (now + TYPING_REFRESH_SECONDS, failures)
        try:
            # A hard deadline also covers a response arriving in very slow chunks.
            async with asyncio.timeout(3.0):
                if not await self.telegram.send_chat_action(chat_id=chat_id, action="typing"):
                    raise TelegramApiError("Telegram did not accept typing status", retryable=True)
        except (TelegramApiError, TimeoutError) as exc:
            failures += 1
            delay = max(TYPING_REFRESH_SECONDS, min(2 ** min(failures, 6), 60),
                        float(getattr(exc, "retry_after", None) or 0))
            self._typing_retry[chat_id] = (time.monotonic() + delay, failures)
            if failures == 1:
                logger.warning("Typing indicator deferred for chat %s: %s", chat_id, type(exc).__name__)
            return
        self._typing_retry[chat_id] = (now + TYPING_REFRESH_SECONDS, 0)
