import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from codex_telegram_bridge import typing_indicator
from codex_telegram_bridge.bridge import BridgeApp
from codex_telegram_bridge.chat_routing import ChatRouting
from codex_telegram_bridge.desktop_client import DesktopClientError, DesktopRequest
from codex_telegram_bridge.state import BridgeState, QueuedInput
from codex_telegram_bridge.telegram_api import TelegramApiError, TelegramBotApi
from test_bridge import make_conversation, make_turn
from test_chat_isolation import POLICY, app


@pytest.fixture(autouse=True)
def mock_actions(app):
    app.telegram.send_chat_action = AsyncMock(return_value=True)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    # Replacing the module reference avoids changing asyncio's own clock.
    monkeypatch.setattr(typing_indicator, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def active(app, task="task-a", chat=-1001, turn_id="telegram-turn"):
    app.state.bind_message(chat, 10, task)
    thread = app.state.threads[task]
    thread.relay_turn_ids = [turn_id]
    app.desktop.threads[task] = make_conversation(task, turns=[make_turn(turn_id, "inProgress")])
    return thread


@pytest.mark.parametrize("task,chat", [("task-a", -1001), ("task-personal", 42)])
async def test_active_telegram_turn_renews_every_four_seconds_without_messages(app, clock, task, chat):
    active(app, task, chat)
    bindings = dict(app.state.message_bindings)
    await app._sync_typing_once()
    clock.now += 3.99
    await app._sync_typing_once()
    app.telegram.send_chat_action.assert_awaited_once_with(chat_id=chat, action="typing")
    clock.now += .01
    await app._sync_typing_once()
    assert app.telegram.send_chat_action.await_count == 2
    app.telegram.send_message.assert_not_called()
    assert app.state.message_bindings == bindings


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted", "waitingForInput", None])
async def test_terminal_or_unknown_status_does_not_type(app, status):
    active(app)
    app.desktop.threads["task-a"].turns[-1].status = status
    await app._sync_typing_once()
    app.telegram.send_chat_action.assert_not_called()


@pytest.mark.parametrize("task,chat", [("task-a", -1001), ("task-personal", 42)])
async def test_new_desktop_turn_does_not_inherit_older_telegram_activity(app, task, chat):
    thread = active(app, task, chat, "old-telegram-turn")
    thread.input_receipts[f"{chat}:10"] = {"status": "confirmed", "turn_id": "old-telegram-turn"}
    thread.current_turn_id = "old-telegram-turn"
    thread.pending_message_ids = [10]
    app.desktop.threads[task].turns.append(make_turn("new-desktop-turn", "inProgress"))
    await app._sync_typing_once()
    app.telegram.send_chat_action.assert_not_called()


async def test_confirmed_receipt_reestablishes_activity_after_restart(app):
    thread = active(app)
    thread.relay_turn_ids = []
    thread.input_receipts["-1001:10"] = {"status": "confirmed", "turn_id": "telegram-turn"}
    app.state.save(app.config.bridge.state_path)
    app.state = BridgeState.load(app.config.bridge.state_path)
    app.state.reset_ephemeral_runtime_state()
    await app._sync_typing_once()
    app.telegram.send_chat_action.assert_awaited_once_with(chat_id=-1001, action="typing")


@pytest.mark.parametrize("key,status", [("-1001:10", "pending"), ("-1002:10", "confirmed")])
async def test_unconfirmed_or_foreign_receipt_cannot_authorize_typing(app, key, status):
    thread = active(app)
    thread.relay_turn_ids = []
    thread.input_receipts[key] = {"status": status, "turn_id": "telegram-turn"}
    await app._sync_typing_once()
    app.telegram.send_chat_action.assert_not_called()


async def test_queued_input_and_stale_current_turn_are_not_active_work(app):
    thread = active(app)
    app.desktop.threads["task-a"].turns = []
    thread.current_turn_id = "telegram-turn"
    thread.queued_inputs = [QueuedInput(-1001, 11, "Next task")]
    thread.pending_message_ids = [11]
    await app._sync_typing_once()
    app.telegram.send_chat_action.assert_not_called()


async def test_exposed_approval_request_suppresses_typing(app):
    active(app)
    app.desktop.threads["task-a"].requests = [DesktopRequest("approve", "command", {})]
    await app._sync_typing_once()
    app.telegram.send_chat_action.assert_not_called()


async def test_unreadable_current_transcript_never_uses_cached_activity(app, clock):
    active(app)
    await app._sync_typing_once()
    clock.now += 10
    app.desktop.read_thread = AsyncMock(side_effect=DesktopClientError("unavailable"))
    await app._sync_typing_once()
    assert app.telegram.send_chat_action.await_count == 1


async def test_detach_while_reading_prevents_late_typing(app):
    active(app)
    original = app.desktop.read_thread
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def delayed_read(thread_id):
        conversation = await original(thread_id)
        entered.set()
        await proceed.wait()
        return conversation

    app.desktop.read_thread = delayed_read
    syncing = asyncio.create_task(app._sync_typing_once())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        app.state.remove_thread("task-a")
        proceed.set()
        await syncing
        app.telegram.send_chat_action.assert_not_called()
    finally:
        syncing.cancel()
        await asyncio.gather(syncing, return_exceptions=True)


async def test_reassigned_chat_is_not_sent_an_activity_update(app):
    thread = active(app)
    thread.primary_chat_id = -1002
    await app._sync_typing_once()
    app.telegram.send_chat_action.assert_not_called()


async def test_multiple_private_tasks_share_one_marker(app):
    policy = copy.deepcopy(POLICY)
    policy["agents"]["personal_two"] = {"thread_id": "task-personal-two", "title": "Personal Two"}
    app.routing = ChatRouting(42, policy)
    active(app, "task-personal", 42, "first")
    active(app, "task-personal-two", 42, "second")
    await app._sync_typing_once()
    app.telegram.send_chat_action.assert_awaited_once_with(chat_id=42, action="typing")


async def test_errors_back_off_without_preventing_other_chat_indicators(app, clock):
    active(app)
    active(app, "task-b", -1002, "other")

    async def send(chat_id, action):
        if chat_id == -1001:
            raise TelegramApiError("temporary", retryable=True)
        return True

    app.telegram.send_chat_action.side_effect = send
    for now in (100, 104, 108, 116):
        clock.now = now
        await app._sync_typing_once()
    assert app._typing_retry[-1001] == (132, 4)
    assert app._typing_retry[-1002] == (120, 0)
    clock.now = 120
    await app._sync_typing_once()
    sent = [call.kwargs["chat_id"] for call in app.telegram.send_chat_action.await_args_list]
    assert sent.count(-1001) == 4 and sent.count(-1002) == 5


async def test_rate_limit_deadline_survives_idle_then_active_transition(app, clock):
    active(app)
    app.telegram.send_chat_action.side_effect = TelegramApiError("limited", retryable=True, retry_after=30)
    await app._sync_typing_once()
    clock.now = 110
    app.desktop.threads["task-a"].turns[-1].status = "completed"
    await app._sync_typing_once()
    active(app, turn_id="next")
    app.telegram.send_chat_action.side_effect = None
    clock.now = 129
    await app._sync_typing_once()
    assert app.telegram.send_chat_action.await_count == 1
    clock.now = 130
    await app._sync_typing_once()
    assert app.telegram.send_chat_action.await_count == 2
    assert app._typing_retry[-1001] == (134, 0)


async def test_slow_chat_does_not_hold_other_chat_action(app):
    active(app)
    active(app, "task-b", -1002, "other")
    slow_entered, release_slow, fast_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(chat_id, action):
        if chat_id == -1001:
            slow_entered.set()
            await release_slow.wait()
        else:
            fast_done.set()
        return True

    app.telegram.send_chat_action.side_effect = send
    syncing = asyncio.create_task(app._sync_typing_once())
    try:
        await asyncio.wait_for(slow_entered.wait(), 1)
        await asyncio.wait_for(fast_done.wait(), 1)
        assert not syncing.done()
        release_slow.set()
        await syncing
    finally:
        syncing.cancel()
        await asyncio.gather(syncing, return_exceptions=True)


async def test_successful_slow_response_does_not_extend_refresh_past_marker_lifetime(app, clock):
    active(app)

    async def slow_response(**kwargs):
        # Telegram may start the five-second marker before its response arrives.
        clock.now += 2.5
        return True

    app.telegram.send_chat_action.side_effect = slow_response
    await app._sync_typing_once()
    clock.now = 104
    await app._sync_typing_once()
    assert app.telegram.send_chat_action.await_count == 2


async def test_typing_request_has_a_hard_deadline_even_when_transport_stalls(app):
    active(app)
    cancelled = asyncio.Event()

    async def stalled(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    app.telegram.send_chat_action.side_effect = stalled
    await asyncio.wait_for(app._sync_typing_once(), 4)
    assert cancelled.is_set()
    assert app._typing_retry[-1001][1] == 1


@pytest.mark.parametrize("condition", ["not_connected", "shutdown"])
async def test_no_actions_before_connection_or_after_shutdown(app, condition):
    active(app)
    if condition == "not_connected":
        app._bot_id = None
    else:
        await app.stop()
    await app._sync_typing_once()
    app.telegram.send_chat_action.assert_not_called()


async def test_typing_continues_while_main_bridge_is_busy_and_stops_cleanly(app, monkeypatch):
    active(app)
    called = asyncio.Event()
    app.telegram.send_chat_action.side_effect = lambda **kwargs: (called.set() or True)

    async def busy_run(self):
        await self._shutdown.wait()
        await self.close()

    monkeypatch.setattr(BridgeApp, "run", busy_run)
    runner = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(called.wait(), 1)
        background = next(task for task in app._tasks if task.get_name() == "telegram-typing")
        await app.stop()
        await asyncio.wait_for(runner, 1)
        assert background.done() and not app._tasks
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_stop_cancels_in_flight_status_request(app, monkeypatch):
    active(app)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def stalled(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def busy_run(self):
        await self._shutdown.wait()
        await self.close()

    app.telegram.send_chat_action.side_effect = stalled
    monkeypatch.setattr(BridgeApp, "run", busy_run)
    runner = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await app.stop()
        await asyncio.wait_for(runner, 1)
        assert cancelled.is_set() and not app._tasks
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.parametrize("failure", ["startup", "cancel"])
async def test_typing_task_is_cleaned_up_even_without_base_shutdown_handler(app, monkeypatch, failure):
    entered = asyncio.Event()

    async def incomplete_run(self):
        entered.set()
        if failure == "startup":
            raise RuntimeError("startup failed")
        await asyncio.Event().wait()

    monkeypatch.setattr(BridgeApp, "run", incomplete_run)
    runner = asyncio.create_task(app.run())
    await asyncio.wait_for(entered.wait(), 1)
    if failure == "startup":
        with pytest.raises(RuntimeError, match="startup failed"):
            await runner
    else:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
    assert not app._tasks


async def test_send_chat_action_uses_telegram_payload_and_short_transport_timeout(app):
    api = TelegramBotApi("123456:fake")
    api._call = AsyncMock(return_value=True)
    try:
        assert await api.send_chat_action(chat_id=-1001)
        assert api._call.call_args.args == ("sendChatAction", {"chat_id": -1001, "action": "typing"})
        timeout = api._call.call_args.kwargs["timeout"]
        assert timeout.connect == timeout.read == timeout.write == timeout.pool == 3
    finally:
        await api.close()
