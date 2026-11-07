import asyncio
from unittest.mock import AsyncMock

import pytest

from codex_telegram_bridge.desktop_client import DesktopQueuedReceipt, DesktopSendUnconfirmedError
from codex_telegram_bridge.state import BridgeState
from codex_telegram_bridge.telegram_api import TelegramApiError
from test_chat_isolation import app, message
from test_retry_delivery import received


def reactions(app):
    return [c.kwargs['emoji'] for c in app.telegram.set_message_reaction.call_args_list]


async def enqueue(app):
    app.desktop.supports_native_queue = True
    app.desktop.threads['task-a'] = received('Existing task', turn_id='before-queue')
    app.desktop.send_message = AsyncMock(return_value=DesktopQueuedReceipt('native-1'))
    await app._handle_telegram_message(message('/ask A correction'))
    return app.state.threads['task-a']


async def test_native_queue_is_not_seen_then_real_turn_is_seen_and_reply_is_done(app):
    thread = await enqueue(app)
    assert reactions(app) == ['😴']
    assert thread.input_receipts['-1001:10']['status'] == 'queued'
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    await app._flush_reactions(thread)
    assert reactions(app) == ['😴']  # Confirmed reactions survive restart.
    app.desktop.threads['task-a'] = received(thread.queued_inputs[0].text)
    await app._attempt_queued_input(thread)
    assert reactions(app) == ['😴', '👀']
    await app._mark_pending_messages_done(thread)
    assert reactions(app) == ['😴', '👀']  # No final reply has reached Telegram yet.
    turn = app.desktop.threads['task-a'].latest_turn
    turn.status = 'completed'
    turn.items.append({'type': 'agentMessage', 'id': 'answer', 'text': 'Correction applied.'})
    turn.raw.update(status='completed', items=turn.items)
    await app._sync_thread('task-a')
    assert reactions(app) == ['😴', '👀', '👌']
    assert not thread.pending_message_ids
    app.desktop.send_message.assert_awaited_once()
    app.telegram.send_message.assert_awaited_once()


async def test_uncertain_click_and_unrelated_active_turn_stay_queued(app):
    app.desktop.supports_native_queue = True
    app.desktop.send_message = AsyncMock(side_effect=DesktopSendUnconfirmedError(
        thread_id='task-a', expected_text='anything', after_turn_count=1))
    await app._handle_telegram_message(message('/ask Correct this'))
    thread = app.state.threads['task-a']
    assert thread.queued_inputs[0].phase == 'verify'
    await app._mark_pending_messages_done(thread)
    assert reactions(app) == ['😴']
    assert thread.pending_message_ids == [10]


async def test_reaction_persisted_before_api_and_transient_failure_retries_after_restart(app, monkeypatch):
    now = [100]
    monkeypatch.setattr('codex_telegram_bridge.delivery_reactions.time.time', lambda: now[0])
    async def fail(**kwargs):
        saved = BridgeState.load(app.config.bridge.state_path)
        assert saved.threads['task-a'].reaction_deliveries['-1001:10']['stage'] == 'queued'
        assert saved.threads['task-a'].queued_inputs
        raise TelegramApiError('Rate limited', retryable=True, retry_after=45)
    app.telegram.set_message_reaction.side_effect = fail
    await enqueue(app)
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    entry = thread.reaction_deliveries['-1001:10']
    assert entry['status'] == 'pending' and entry['next_at'] == 145
    # Exact-turn confirmation upgrades desired state, but preserves rate limiting.
    app.desktop.threads['task-a'] = received(thread.queued_inputs[0].text)
    await app._attempt_queued_input(thread)
    assert reactions(app) == ['😴']
    assert thread.reaction_deliveries['-1001:10']['stage'] == 'seen'
    now[0] = 145
    app.telegram.set_message_reaction.side_effect = None
    await app._flush_reactions(thread)
    assert reactions(app) == ['😴', '👀']
    assert thread.reaction_deliveries['-1001:10']['status'] == 'sent'


async def test_slow_queued_reaction_cannot_overwrite_newer_seen_state(app):
    thread = await enqueue(app)
    thread.reaction_deliveries.clear()
    app.telegram.set_message_reaction.reset_mock()
    started, finish = asyncio.Event(), asyncio.Event()
    async def slow(**kwargs):
        if kwargs['emoji'] == '😴':
            started.set()
            await finish.wait()
        return True
    app.telegram.set_message_reaction.side_effect = slow
    app._record_reaction(thread, -1001, 10, 'queued')
    task = asyncio.create_task(app._flush_reactions(thread))
    await started.wait()
    app._record_reaction(thread, -1001, 10, 'seen')
    await app._flush_reactions(thread)
    finish.set()
    await task
    app._record_reaction(thread, -1001, 10, 'queued')
    await app._flush_reactions(thread)
    assert reactions(app) == ['😴', '👀']
    assert thread.reaction_deliveries['-1001:10']['stage'] == 'seen'


@pytest.mark.parametrize('error', [TimeoutError(), TelegramApiError('Unknown outcome', uncertain=True)])
async def test_ambiguous_reaction_is_safe_to_retry(app, error):
    app.telegram.set_message_reaction.side_effect = error
    thread = await enqueue(app)
    entry = thread.reaction_deliveries['-1001:10']
    assert entry['status'] == 'pending'
    entry['next_at'] = 0
    app.telegram.set_message_reaction.side_effect = None
    await app._flush_reactions(thread)
    assert entry['status'] == 'sent'


async def test_permanent_reaction_failure_does_not_stop_input_or_repeat_forever(app):
    app.telegram.set_message_reaction.side_effect = TelegramApiError('Reactions disabled', error_code=400)
    thread = await enqueue(app)
    assert thread.input_receipts['-1001:10']['status'] == 'queued'
    assert thread.reaction_deliveries['-1001:10']['status'] == 'blocked'
    await app._flush_reactions(thread)
    assert reactions(app) == ['😴']


async def test_no_reaction_without_bound_authorized_source_message(app):
    thread = await enqueue(app)
    app.telegram.set_message_reaction.reset_mock()
    app._record_reaction(thread, -1002, 20, 'seen')
    app._record_reaction(thread, -1001, 999, 'seen')
    await app._flush_reactions(thread)
    app.telegram.set_message_reaction.assert_not_called()


async def test_migrates_legacy_waiting_input_without_pretending_it_is_seen(app):
    thread = await enqueue(app)
    thread.reaction_deliveries.clear()
    app.telegram.set_message_reaction.reset_mock()
    await app._sync_all_threads_once()
    assert reactions(app) == ['😴']
