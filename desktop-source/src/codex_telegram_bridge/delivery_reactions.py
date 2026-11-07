"""Persistent Telegram receipt reactions; queue acceptance is not agent receipt."""
from __future__ import annotations

import asyncio
import logging
import time

from .retry_delivery import retry_delay
from .telegram_api import TelegramApiError

logger = logging.getLogger(__name__)
STAGES = {"queued": 0, "seen": 1, "done": 2}


class DeliveryReactionsMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reaction_locks = {}

    def _record_reaction(self, thread, chat_id, message_id, stage):
        """Caller saves this with the corresponding input/output receipt."""
        key = f"{chat_id}:{message_id}"
        entry = thread.reaction_deliveries.get(key)
        if entry and STAGES[entry["stage"]] >= STAGES[stage]:
            return False
        # Keep any server retry_after across stage changes.
        thread.reaction_deliveries[key] = {
            "stage": stage, "status": "pending", "attempts": 0,
            "next_at": (entry or {}).get("next_at", 0),
        }
        return True

    async def _flush_reactions(self, thread):
        lock = self._reaction_locks.setdefault(thread.thread_id, asyncio.Lock())
        if lock.locked():
            return  # The active flusher will observe subsequent stage changes.
        async with lock:
            for key in list(thread.reaction_deliveries):
                # At most three forward transitions can occur during one send.
                for _ in STAGES:
                    entry = thread.reaction_deliveries[key]
                    if entry["status"] != "pending" or entry.get("next_at", 0) > time.time():
                        break
                    chat_id, message_id = map(int, key.split(":"))
                    if (self.state.threads.get(thread.thread_id) is not thread
                            or not self.routing.allowed(chat_id, thread.thread_id)
                            or self.state.lookup_thread_for_message(chat_id, message_id) != thread.thread_id):
                        break
                    emoji = getattr(self.config.telegram, entry["stage"] + "_reaction")
                    try:
                        async with asyncio.timeout(3):
                            accepted = await self.telegram.set_message_reaction(
                                chat_id=chat_id, message_id=message_id, emoji=emoji or None)
                        if not accepted:
                            raise TelegramApiError("Reaction not accepted", retryable=True)
                    except (TelegramApiError, TimeoutError, OSError) as exc:
                        # Setting a reaction replaces the bot's previous reaction;
                        # even a timeout is safe to retry without duplication.
                        entry["attempts"] += 1
                        entry["error"] = str(exc) or type(exc).__name__
                        permanent = isinstance(exc, TelegramApiError) and not (exc.retryable or exc.uncertain)
                        entry["status"] = "blocked" if permanent else "pending"
                        entry["next_at"] = time.time() + retry_delay(entry["attempts"], getattr(exc, "retry_after", None))
                        current = thread.reaction_deliveries[key]
                        if current is not entry:
                            current["next_at"] = max(current.get("next_at", 0), entry["next_at"])
                        logger.warning("Reaction %s %s for %s: %s", entry["stage"], entry["status"], key, entry["error"])
                        await self._save_state()
                        break
                    entry["status"] = "sent"
                    entry["next_at"] = 0
                    entry.pop("error", None)
                    await self._save_state()
                    if thread.reaction_deliveries[key] is entry:
                        break

    async def _sync_all_threads_once(self):
        # Also migrate still-pending messages saved by older bridge versions.
        changed = False
        for thread in list(self.state.threads.values()):
            for job in thread.queued_inputs:
                changed = self._record_reaction(thread, job.chat_id, job.message_id, "queued") or changed
            for mid in thread.pending_message_ids:
                chat_id = thread.primary_chat_id
                receipt = thread.input_receipts.get(f"{chat_id}:{mid}", {})
                if receipt.get("status") == "confirmed":
                    sent = thread.output_deliveries.get(receipt.get("turn_id"), {}).get("status") == "sent"
                    changed = self._record_reaction(thread, chat_id, mid, "done" if sent else "seen") or changed
        if changed:
            await self._save_state()
        await asyncio.gather(*(self._flush_reactions(thread) for thread in list(self.state.threads.values())))
        await super()._sync_all_threads_once()
