from __future__ import annotations

import logging
import json
import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
LONG_POLL_TIMEOUT_SLACK_SECONDS = 5.0
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class TelegramApiError(RuntimeError):
    def __init__(self, message, *, retryable=False, uncertain=False, retry_after=None, error_code=None, migrate_to_chat_id=None):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain
        self.retry_after = retry_after
        self.error_code = error_code
        self.migrate_to_chat_id = migrate_to_chat_id


class TelegramFileTooLarge(TelegramApiError):
    pass


@dataclass(slots=True)
class SentMessage:
    chat_id: int
    message_id: int
    raw: dict[str, Any]


class TelegramBotApi:
    def __init__(self, bot_token: str, *, base_url: str = "https://api.telegram.org") -> None:
        self._base_url = base_url.rstrip("/")
        self._bot_token = bot_token
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_REQUEST_TIMEOUT_SECONDS, connect=DEFAULT_CONNECT_TIMEOUT_SECONDS)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        assert isinstance(result, dict)
        return result

    async def get_updates(
        self,
        *,
        offset: int,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        request_timeout = httpx.Timeout(
            float(timeout) + LONG_POLL_TIMEOUT_SLACK_SECONDS,
            connect=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )
        result = await self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": allowed_updates or ["message", "callback_query"],
            },
            timeout=request_timeout,
        )
        assert isinstance(result, list)
        return result

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        disable_notification: bool = False,
    ) -> SentMessage:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        if entities:
            payload["entities"] = entities
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        result = await self._call("sendMessage", payload)
        return SentMessage(
            chat_id=int(result["chat"]["id"]),
            message_id=int(result["message_id"]),
            raw=result,
        )

    async def delete_message(self, *, chat_id: int, message_id: int) -> bool:
        return bool(await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))

    async def send_chat_action(self, *, chat_id: int, action: str = "typing") -> bool:
        # Ephemeral status must not hold up actual messages when Telegram is slow.
        return bool(await self._call("sendChatAction", {"chat_id": chat_id, "action": action},
                                     timeout=httpx.Timeout(3.0, connect=3.0)))

    async def download_file(self, *, file_id: str, destination: Path, expected_size: int | None = None) -> Path:
        if expected_size is not None and expected_size > MAX_DOWNLOAD_BYTES:
            raise TelegramFileTooLarge("Telegram bot downloads are limited to 20 MB")
        info = await self._call("getFile", {"file_id": file_id})
        remote = info.get("file_path") if isinstance(info, dict) else None
        if not isinstance(remote, str) or not remote or PurePosixPath(remote).is_absolute() or ".." in PurePosixPath(remote).parts or "\\" in remote:
            raise TelegramApiError("Telegram returned an invalid file path")
        if int(info.get("file_size") or 0) > MAX_DOWNLOAD_BYTES:
            raise TelegramFileTooLarge("Telegram bot downloads are limited to 20 MB")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(destination.name + ".partial")
        url = f"{self._base_url}/file/bot{self._bot_token}/{quote(remote, safe='/')}"
        count = 0
        try:
            async with self._client.stream("GET", url, timeout=120, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise TelegramApiError(f"Telegram file download failed (HTTP {response.status_code})")
                with temporary.open("xb") as handle:
                    os.chmod(temporary, 0o600)
                    async for block in response.aiter_bytes():
                        count += len(block)
                        if count > MAX_DOWNLOAD_BYTES:
                            raise TelegramFileTooLarge("Telegram bot downloads are limited to 20 MB")
                        handle.write(block)
            known_size = info.get("file_size", expected_size)
            if known_size is not None and count != int(known_size):
                raise TelegramApiError("Telegram file download was incomplete")
            temporary.replace(destination)
            return destination
        except httpx.HTTPError as exc:
            raise TelegramApiError(f"Telegram file download failed: {type(exc).__name__}") from None
        finally:
            temporary.unlink(missing_ok=True)

    async def send_file(self, *, chat_id: int, path: Path, kind: str = "document", reply_to_message_id: int | None = None) -> SentMessage:
        methods = {"photo": ("sendPhoto", "photo"), "video": ("sendVideo", "video"), "audio": ("sendAudio", "audio"), "animation": ("sendAnimation", "animation"), "document": ("sendDocument", "document")}
        method, field = methods[kind]
        # An open descriptor pins the inspected file; never follow a last-component symlink.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise TelegramApiError("Only regular files can be uploaded")
            limit = 10 * 1024 * 1024 if kind == "photo" else MAX_UPLOAD_BYTES
            if info.st_size > limit:
                raise TelegramFileTooLarge(f"Telegram upload exceeds the {limit // (1024 * 1024)} MB limit")
            payload = {"chat_id": str(chat_id), "caption": path.name[:900]}
            if reply_to_message_id is not None:
                payload["reply_parameters"] = json.dumps({"message_id": reply_to_message_id})
            if kind == "video":
                payload["supports_streaming"] = "true"
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            try:
                response = await self._client.post(f"{self._base_url}/bot{self._bot_token}/{method}", data=payload,
                    files={field: (path.name, handle, mime)}, timeout=httpx.Timeout(180, connect=10))
            except httpx.HTTPError as exc:
                raise TelegramApiError(f"Telegram upload could not be confirmed: {type(exc).__name__}") from None
            result = self._decode_response(response, method)
            return SentMessage(int(result["chat"]["id"]), int(result["message_id"]), result)

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return bool(await self._call("answerCallbackQuery", payload))

    async def set_message_reaction(self, *, chat_id: int, message_id: int, emoji: str | None) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if emoji:
            payload["reaction"] = [{"type": "emoji", "emoji": emoji}]
        else:
            payload["reaction"] = []
        return bool(await self._call("setMessageReaction", payload))

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: httpx.Timeout | float | None = None,
    ) -> Any:
        url = f"{self._base_url}/bot{self._bot_token}/{method}"
        try:
            response = await self._client.post(url, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            # Telegram uses the credential in its URL; never log an exception URL.
            detail = exc.__class__.__name__
            safe = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            raise TelegramApiError(f"Telegram API request failed for {method}: {detail}",
                retryable=safe, uncertain=not safe) from None
        return self._decode_response(response, method)

    def _decode_response(self, response: httpx.Response, method: str) -> Any:
        try:
            data = response.json()
        except ValueError:
            data = None

        if isinstance(data, dict) and not data.get("ok", False):
            description = str(data.get("description") or f"Telegram API call failed: {method}").replace(self._bot_token, "[redacted]")
            error_code = data.get("error_code")
            parameters = data.get('parameters')
            parameters = parameters if isinstance(parameters, dict) else {}
            retry_after = parameters.get('retry_after')
            migrate_to = parameters.get('migrate_to_chat_id')
            if type(migrate_to) is not int or migrate_to >= 0:
                migrate_to = None
            retryable = isinstance(error_code, int) and (error_code == 429 or error_code >= 500)
            if error_code is not None:
                raise TelegramApiError(f"{description} (error_code={error_code}, http_status={response.status_code})",
                    retryable=retryable, retry_after=retry_after, error_code=error_code,
                    migrate_to_chat_id=migrate_to)
            raise TelegramApiError(f"{description} (http_status={response.status_code})")

        if response.is_error:
            detail = f"HTTP {response.status_code}"
            raise TelegramApiError(f"Telegram API request failed for {method}: {detail}", uncertain=True)

        if not isinstance(data, dict):
            raise TelegramApiError(f"Telegram API returned a non-JSON response for {method}", uncertain=True)

        return data.get("result")


__all__ = ["SentMessage", "TelegramApiError", "TelegramBotApi"]
