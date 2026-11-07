"""Delivery delays must not stop intake or another agent's outgoing reply."""
import asyncio
from types import MethodType
from unittest.mock import AsyncMock

import pytest

from codex_telegram_bridge.desktop_client import DesktopBusyError, DesktopQueuedReceipt
from codex_telegram_bridge.local_mode import LocalBridgeApp
from codex_telegram_bridge.state import BridgeState
from test_chat_isolation import app, message
from test_retry_delivery import received


@pytest.fixture
def background_app(app):
    app._dispatch_queued_input = MethodType(LocalBridgeApp._dispatch_queued_input, app)
    return app


async def drain(app):
    await asyncio.gather(*list(app._input_delivery_tasks.values()))
    await asyncio.sleep(0)  # Run task completion callbacks.


async def test_slow_input_does_not_block_followups_or_another_agents_output(background_app):
    app = background_app
    app.desktop.supports_native_queue = True
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_send(task, text, **kwargs):
        if task == 'task-a':
            started.set()
            await release.wait()
        return DesktopQueuedReceipt('native-' + task)

    app.desktop.send_message = AsyncMock(side_effect=slow_send)
    async with asyncio.timeout(1):
        await app._handle_telegram_message(message('/ask First', message_id=10))
        await started.wait()
        await app._handle_telegram_message(message('/ask Correction', message_id=11))
        # Simulate a different agent finishing while A is still submitting.
        await app._handle_telegram_message(message('/ask B', chat=-1002, user=202, message_id=12))
        b = app.state.threads['task-b']
        turn = received('B', turn_id='b-final', status='completed').latest_turn
        turn.items.append({'type': 'agentMessage', 'id': 'b-answer', 'text': 'B finished'})
        turn.raw['items'] = turn.items
        b.relay_turn_ids.append('b-final')
        await app._deliver_terminal_turn(b, turn)
        saved = BridgeState.load(app.config.bridge.state_path)
        assert [p.message_id for p in saved.threads['task-a'].queued_inputs] == [10, 11]
        assert any(c.kwargs['text'] == 'B finished' and c.kwargs['chat_id'] == -1002
                   for c in app.telegram.send_message.call_args_list)
        assert not release.is_set()
    release.set()
    await drain(app)
    a_calls = [c for c in app.desktop.send_message.call_args_list if c.args[0] == 'task-a']
    assert len(a_calls) == 1  # Later input cannot overtake an unconfirmed submission.
    await app._dispatch_queued_input(app.state.threads['task-a'])
    await drain(app)
    assert [p.phase for p in app.state.threads['task-a'].queued_inputs] == ['native_queued'] * 2
    assert len([c for c in app.desktop.send_message.call_args_list if c.args[0] == 'task-a']) == 2


async def test_shutdown_preserves_uncertain_click_for_verification(background_app):
    app = background_app
    clicked = asyncio.Event()

    async def uncertain(task, text, *, checkpoint, **kwargs):
        await checkpoint('verify')
        clicked.set()
        await asyncio.Event().wait()

    app.desktop.send_message = AsyncMock(side_effect=uncertain)
    await app._handle_telegram_message(message('/ask First'))
    await clicked.wait()
    await app.close()
    saved = BridgeState.load(app.config.bridge.state_path)
    assert saved.threads['task-a'].queued_inputs[0].phase == 'verify'
    assert not app._input_delivery_tasks
    app.desktop.send_message.assert_awaited_once()


async def test_desktop_wait_notice_is_once_survives_restart_and_does_not_claim_seen(app, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('codex_telegram_bridge.retry_delivery.time.time', lambda: clock[0])
    app.desktop.send_message = AsyncMock(side_effect=DesktopBusyError('Reserved'))
    await app._handle_telegram_message(message('/ask First'))
    thread = app.state.threads['task-a']
    assert thread.queued_inputs[0].desktop_wait_since == 100
    app.telegram.send_message.assert_not_called()
    clock[0] = 161
    await app._attempt_queued_input(thread)
    assert 'has not reached the agent yet' in app.telegram.send_message.call_args.kwargs['text']
    assert app.telegram.send_message.call_args.kwargs['chat_id'] == -1001
    assert thread.queued_inputs[0].attempts == 0
    assert thread.reaction_deliveries['-1001:10']['stage'] == 'queued'
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    clock[0] = 1000
    await app._attempt_queued_input(thread)
    app.telegram.send_message.assert_awaited_once()
    app.desktop.send_message.side_effect = None
    app.desktop.send_message.return_value = received(thread.queued_inputs[0].text)
    clock[0] += 3
    await app._attempt_queued_input(thread)
    assert not thread.queued_inputs
    assert thread.input_receipts['-1001:10']['status'] == 'confirmed'


async def test_unexpected_background_failure_leaves_saved_message_retryable(background_app, caplog):
    app = background_app
    app.desktop.send_message = AsyncMock(side_effect=RuntimeError('unexpected failure'))
    await app._handle_telegram_message(message('/ask First'))
    tasks = list(app._input_delivery_tasks.values())
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert not app._input_delivery_tasks
    assert not app._threads_with_send_in_flight
    assert BridgeState.load(app.config.bridge.state_path).threads['task-a'].queued_inputs
    assert 'saved input retained' in caplog.text


async def test_waiting_to_verify_click_never_claims_agent_did_not_receive_it(app, monkeypatch):
    from codex_telegram_bridge.state import QueuedInput
    clock = [100.0]
    monkeypatch.setattr('codex_telegram_bridge.retry_delivery.time.time', lambda: clock[0])
    app.desktop.supports_native_queue = True
    await app._attach_group(-1001, 'task-a')
    thread = app.state.threads['task-a']
    thread.queued_inputs = [QueuedInput(-1001, 10, 'First', phase='verify', attempts=1)]
    app.desktop.read_native_queue = AsyncMock(side_effect=DesktopBusyError('Reserved'))
    app.desktop.send_message = AsyncMock()
    app.telegram.send_message.reset_mock()
    await app._attempt_queued_input(thread)
    clock[0] = 161
    await app._attempt_queued_input(thread)
    notice = app.telegram.send_message.call_args.kwargs['text']
    assert 'has not been confirmed yet' in notice
    assert 'has not reached' not in notice
    assert thread.queued_inputs[0].phase == 'verify'
    app.desktop.send_message.assert_not_awaited()
