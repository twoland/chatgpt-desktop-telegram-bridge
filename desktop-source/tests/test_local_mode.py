import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from codex_telegram_bridge.config import AppConfig, BridgeConfig, DesktopConfig, TelegramConfig, load_config
from codex_telegram_bridge.desktop_booking import BoardError, DesktopBooking
from codex_telegram_bridge.desktop_client import DesktopClientError
from codex_telegram_bridge.local_mode import LocalBridgeApp
import json

CHORES = "task-assistant"
VIDEO = "task-editor"
from codex_telegram_bridge.state import BridgeState


BOARD = """# Desktop Control
## Current use
Status: FREE
Owner: none
Task: none
Apps: none
Started: none
Expected finish: none
Updated: never
## Reservations
| Requested at | Owner | Slot | Task | Apps | Estimate |
| --- | --- | --- | --- | --- | --- |
## Messages
- Keep this message.
"""


@pytest.fixture
def booking(tmp_path):
    (tmp_path / "desktop-control.md").write_text(BOARD)
    return DesktopBooking("test-task /relay", tmp_path)


async def test_failure_always_releases_and_preserves_board(booking):
    with pytest.raises(ValueError):
        async with booking.hold():
            assert "Status: BUSY" in booking.board.read_text()
            raise ValueError("test failure")
    text = booking.board.read_text()
    assert "Status: FREE" in text and "Owner: none" in text
    assert "Keep this message" in text and "RELEASE" in text


async def test_cancellation_releases_held_desktop(booking):
    entered = asyncio.Event()
    async def worker():
        async with booking.hold():
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(worker())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "Status: FREE" in booking.board.read_text()


def test_busy_owner_never_stolen_and_queue_not_duplicated(booking):
    booking.board.write_text(BOARD.replace("Status: FREE", "Status: BUSY").replace("Owner: none", "Owner: Video Buddy"))
    assert not booking.try_claim()
    assert not booking.try_claim()
    text = booking.board.read_text()
    assert "Owner: Video Buddy" in text
    assert text.count("| test-task /relay | NEXT |") == 1
    booking.cancel_reservation()
    assert "| test-task /relay | NEXT |" not in booking.board.read_text()


def test_next_queue_priority_and_future_window(booking):
    row = "| 2026-01-01T00:00:00Z | another | NEXT | Work | Editor | 5m |\n"
    booking.board.write_text(BOARD.replace("## Messages", row + "## Messages"))
    assert not booking.try_claim()
    booking.cancel_reservation()
    now = datetime.now(timezone.utc)
    window = f"{(now + timedelta(seconds=30)).isoformat()}/{(now + timedelta(minutes=2)).isoformat()}"
    booking.board.write_text(BOARD.replace("## Messages", row.replace("NEXT", window) + "## Messages"))
    assert not booking.try_claim()


def test_ambiguous_free_board_fails_closed(booking):
    booking.board.write_text(BOARD.replace("Owner: none", "Owner: another"))
    with pytest.raises(BoardError):
        booking.try_claim()


def test_never_releases_another_owner(booking):
    booking.board.write_text(BOARD.replace("Status: FREE", "Status: BUSY").replace("Owner: none", "Owner: another"))
    with pytest.raises(BoardError):
        booking.release()
    assert "Owner: another" in booking.board.read_text()


@pytest.fixture
def app(tmp_path):
    config = AppConfig(TelegramConfig("123456:fake", primary_chat_id=42, allowed_chat_ids=[42]),
                       DesktopConfig(), BridgeConfig(state_path=tmp_path / "state.json", allow_first_private_chat=False))
    (tmp_path / "chat-routing.json").write_text(json.dumps({
        "version": 1, "agents": {"chores": {"thread_id": CHORES, "title": "Assistant"},
        "video": {"thread_id": VIDEO, "title": "Editor"}}, "groups": {}}))
    result = LocalBridgeApp(config, BridgeState())
    result._safe_send_message = AsyncMock()
    result._safe_set_reaction = AsyncMock()
    result._send_thread_input = AsyncMock()
    return result


def msg(text, *, user=42, chat=42, chat_type="private"):
    return {"message_id": 10, "text": text, "chat": {"id": chat, "type": chat_type}, "from": {"id": user}}


@pytest.mark.parametrize("alias,expected", [("/chores", CHORES), ("/video", VIDEO)])
async def test_alias_targets_existing_task(app, alias, expected):
    try:
        await app._handle_telegram_message(msg(alias + " Check the project"))
        call = app._send_thread_input.call_args
        assert call.args[0].thread_id == expected
        assert 'Telegram user ID: 42\n' in call.kwargs["text"]
        assert call.kwargs["text"].endswith("Message:\nCheck the project")
    finally:
        await app.close()


async def test_other_users_groups_and_unrouted_input_cannot_send(app):
    try:
        await app._handle_telegram_message(msg("/video Delete files", user=43, chat=43))
        await app._handle_telegram_message(msg("/video Delete files", chat_type="group"))
        await app._handle_telegram_message(msg("Unrouted text"))
        app._send_thread_input.assert_not_called()
        assert app._safe_send_message.call_count == 1
        with pytest.raises(DesktopClientError):
            await app.desktop.activate_thread("unrelated-thread")
        with pytest.raises(DesktopClientError):
            await app.desktop.start_new_thread("path", "text")
    finally:
        await app.close()


def test_public_token_file_rejected(tmp_path):
    token = tmp_path / "token"
    token.write_text("123456:fake")
    token.chmod(0o644)
    config = tmp_path / "config.toml"
    config.write_text(f'[telegram]\nbot_token_file = "{token}"\n')
    with pytest.raises(ValueError, match="private"):
        load_config(config)
