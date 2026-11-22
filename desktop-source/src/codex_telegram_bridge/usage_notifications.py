"""Read-only account quota checks and durable, owner-only Telegram notices."""
from __future__ import annotations

import asyncio
import logging
import math
import shutil
import time
from datetime import datetime

from .codex_rpc import CodexAppServerClient
from .telegram_api import TelegramApiError

logger = logging.getLogger(__name__)


def usage_command(config):
    if config.bridge.usage_command is not None:
        return list(config.bridge.usage_command)
    resources = config.desktop.app_path / "Contents/Resources"
    for path in (resources / "codex-cli/bin/codex", resources / "codex"):
        if path.is_file():
            return [str(path), "app-server"]
    executable = shutil.which("codex")
    if executable:
        return [executable, "app-server"]
    raise FileNotFoundError("Configure bridge.usage_command with a signed-in Codex app-server")


async def read_usage(config):
    """A short-lived app-server; never start a thread, consume resets or buy credits."""
    async def notification(*args):
        pass

    async def request(method, request_id, params):
        await client.respond_error(request_id, -32601, "Usage reader does not handle server requests")

    client = CodexAppServerClient(
        usage_command(config), client_name="telegram_bridge_usage",
        client_title="Telegram Bridge usage notifications", client_version="0.1",
        experimental_api=False, opt_out_notification_methods=[],
        notification_handler=notification, request_handler=request,
    )
    try:
        async with asyncio.timeout(20):
            await client.start()
            return await client.request("account/rateLimits/read")
    finally:
        await client.close()


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def usage_observations(payload):
    """Missing buckets/windows are unknown, never evidence of recovery."""
    if not isinstance(payload, dict):
        return {}
    observations = {}
    allowed = payload.get("ordinaryUsageAllowed")
    if isinstance(allowed, bool):
        observations["account"] = {"limited": not allowed, "label": "Codex account", "resets_at": None}
    buckets = payload.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = payload.get("rateLimits")
        buckets = {str(legacy.get("limitId") or "codex"): legacy} if isinstance(legacy, dict) else {}
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        known = {name: bucket[name] for name in ("primary", "secondary")
                 if isinstance(bucket.get(name), dict) and number(bucket[name].get("usedPercent"))
                 and bucket[name]["usedPercent"] >= 0}
        exhausted = {name: w for name, w in known.items() if w["usedPercent"] >= 100}
        reached = bucket.get("rateLimitReachedType")
        explicit = (isinstance(reached, str) and bool(reached.strip())) or bucket.get("spendControlReached") is True
        # A positive availability result takes precedence over rounded usage
        # percentages (or plan quotas covered by credits). Explicit bucket-level
        # limits may still apply to a particular model.
        limited = explicit or (bool(exhausted) and allowed is not True)
        if not limited and (not known or exhausted):
            continue
        resets = [w["resetsAt"] for w in exhausted.values() if number(w.get("resetsAt"))
                  and 0 < w["resetsAt"] < 253402300800]
        label = bucket.get("limitName") or bucket.get("limitId") or key
        label = " ".join(str(label).split())[:80]
        observations[f"bucket:{key}"] = {
            "limited": bool(limited), "label": label or "Codex quota",
            "resets_at": max(resets) if resets else None,
            "checked_windows": list(known), "exhausted_windows": list(exhausted),
        }
    return observations


class UsageNotifications:
    def __init__(self, config, state, *, send, save, read=read_usage, clock=time.time):
        self.config, self.state = config, state
        self.send, self.save, self.read, self.clock = send, save, read, clock
        # Only the explicitly paired owner's private DM, never a group, inferred
        # first chat, routing alias or incoming message's claimed destination.
        self.owner = config.telegram.primary_chat_id
        self.enabled = config.bridge.usage_notifications and isinstance(self.owner, int) and self.owner > 0
        self.data = state.usage_notifications
        if self.enabled and self.data.get("owner") != self.owner:
            self.data.clear()  # Never carry account notices into a newly paired DM.
            self.data["owner"] = self.owner
        notice = self.data.get("notice") or {}
        if notice.get("status") == "sending":
            notice["status"] = "uncertain"  # Do not duplicate an interrupted upload.

    async def run(self, shutdown):
        if not self.enabled:
            return
        while not shutdown.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Usage notification check held (%s); will retry", type(exc).__name__)
            try:
                await asyncio.wait_for(shutdown.wait(), self.config.bridge.usage_poll_interval_seconds)
            except TimeoutError:
                pass

    async def tick(self):
        if not self.enabled:
            return
        try:
            payload = await self.read(self.config)
            await self.observe(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not log account responses, local paths or authentication errors.
            logger.warning("Usage check unavailable (%s); retaining last known quota state", type(exc).__name__)
        await self.deliver()

    async def observe(self, payload):
        observations = usage_observations(payload)
        if not observations:
            return
        active = dict(self.data.get("active") or {})
        was_limited = bool(active)
        for key, observation in observations.items():
            if observation["limited"]:
                active[key] = observation
            else:
                required = set((active.get(key) or {}).get("exhausted_windows") or [])
                if required <= set(observation.get("checked_windows") or []):
                    active.pop(key, None)
        self.data["active"] = active
        self.data["checked_at"] = self.clock()
        if bool(active) != was_limited:
            previous = self.data.get("notice") or {}
            if active:
                self.data["notice"] = {
                    "kind": "limited", "text": self.limit_text(active), "status": "pending", "attempts": 0,
                }
            elif previous.get("status") in {"sent", "sending", "uncertain"} or previous.get("kind") == "recovered":
                self.data["notice"] = {
                    "kind": "recovered", "status": "pending", "attempts": 0,
                    "text": "Codex reports that the usage limit has cleared. Saved Telegram messages remain eligible for normal delivery retries. If the desktop still shows an old limit banner, refresh the app view.",
                }
            else:
                self.data.pop("notice", None)  # No recovery spam for a warning never delivered.
        await self.save()

    @staticmethod
    def limit_text(active):
        lines = ["Codex has reached a usage limit."]
        labels = sorted({item["label"] for key, item in active.items() if key != "account"})
        if labels:
            lines.append("Affected quota: " + ", ".join(labels[:8]) + (" and others." if len(labels) > 8 else "."))
        resets = [item["resets_at"] for item in active.values() if item["resets_at"]]
        if resets:
            reported = datetime.fromtimestamp(max(resets)).astimezone().strftime("%a %d %b, %H:%M %Z")
            lines.append("Reported reset: " + reported + ".")
        lines.append("The bridge is still running; saved messages remain queued for delivery. This notice does not redeem reset credits or purchase anything.")
        return "\n\n".join(lines)

    async def deliver(self):
        entry = self.data.get("notice") or {}
        if entry.get("status") != "pending" or entry.get("next_at", 0) > self.clock():
            return
        entry["status"] = "sending"
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        await self.save()  # Persist before the consequential send.
        try:
            sent = await self.send(chat_id=self.owner, text=entry["text"])
        except TelegramApiError as exc:
            if exc.uncertain:
                entry["status"] = "uncertain"
            elif exc.retryable:
                entry["status"] = "pending"
                delay = max(min(2 ** min(entry["attempts"], 12), 3600), float(exc.retry_after or 0))
                entry["next_at"] = self.clock() + delay
            else:
                entry["status"] = "failed"
            logger.warning("Usage notification delivery %s", entry["status"])
        except asyncio.CancelledError:
            raise  # A persisted 'sending' entry becomes uncertain on restart.
        except Exception:
            entry["status"] = "uncertain"
            logger.warning("Usage notification delivery uncertain; automatic resend held")
        else:
            entry["status"] = "sent"
            entry["message_id"] = sent.message_id
            logger.info("Usage notification sent (%s)", entry["kind"])
        await self.save()
