import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from codex_telegram_bridge.chat_routing import ChatRouting
from codex_telegram_bridge.state import BridgeState
from codex_telegram_bridge.telegram_api import SentMessage, TelegramApiError
from test_bridge import make_turn
from test_chat_isolation import POLICY, app, message


def test_default_and_per_chat_reply_modes_do_not_change_routing():
    raw = copy.deepcopy(POLICY)
    assert ChatRouting(42, raw).reply_mode_for(-1001) == 'all'
    raw.update(reply_mode='first', reply_modes={'-1001': 'no', '42': 'all'})
    routing = ChatRouting(42, raw)
    assert [routing.reply_mode_for(c) for c in [-1001, -1002, 42]] == ['no', 'first', 'all']
    assert routing.resolve(-1001, 'editor_b') is None
    assert routing.resolve(42, 'editor_a') is None


@pytest.mark.parametrize('value', [None, True, [], {}, 'off', 'First'])
def test_invalid_default_reply_mode_fails_closed(value):
    with pytest.raises(ValueError, match='Reply mode'):
        ChatRouting(42, {**POLICY, 'reply_mode': value})


@pytest.mark.parametrize('value', [[], {'01': 'no'}, {'-9999': 'first'}, {'202': 'all'},
                                  {'-1001': True}, {'-1001': 'unknown'}])
def test_invalid_override_fails_closed(value):
    with pytest.raises(ValueError, match='[Rr]eply.mode'):
        ChatRouting(42, {**POLICY, 'reply_modes': value})


async def prepare(app, text='Final answer.', source=10, turn_id='output'):
    if 'task-a' not in app.state.threads:
        await app._attach_group(-1001, 'task-a')
    thread = app.state.threads['task-a']
    app.state.bind_message(-1001, source, 'task-a')
    thread.input_receipts[f'-1001:{source}'] = {'status': 'confirmed', 'turn_id': turn_id}
    thread.relay_turn_ids.append(turn_id)
    turn = make_turn(turn_id, 'completed', items=[{'type': 'agentMessage', 'id': turn_id + '-answer', 'text': text}])
    return thread, turn


async def update(app, name, text, source=10):
    folder = app.config.bridge.state_path.parent / 'outbox/requests'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name * 64 + '.json')
    path.write_text(json.dumps({'thread_id': 'task-a', 'chat_id': -1001, 'reply_to': source, 'text': text}))
    await app._sync_all_threads_once()


@pytest.mark.parametrize('mode', ['no', 'first', 'all'])
async def test_updates_split_final_and_files_share_one_request_after_restart(app, tmp_path, mode):
    app.routing.reply_mode = mode
    app.config.bridge.max_message_chars = 30
    path = tmp_path / 'report.txt'
    path.write_text('approved test file')
    app.routing.file_roots['task-a'] = [tmp_path]
    thread, turn = await prepare(app, 'Detailed final answer. ' * 5 + f'[Report](<{path}>)')
    app.telegram.send_file = AsyncMock(return_value=SentMessage(-1001, 950, {}))
    await update(app, 'a', 'Working on it.')
    # Restart between acknowledgment and follow-up, then interleave a newer
    # input: the older final must retain its own source rather than latest input.
    app.state = BridgeState.load(app.config.bridge.state_path)
    app.state.bind_message(-1001, 11, 'task-a')
    thread = app.state.threads['task-a']
    thread.pending_message_ids = [10, 11]
    await update(app, 'b', 'Progress update.')
    assert await app._deliver_terminal_turn(thread, turn)
    calls = app.telegram.send_message.call_args_list
    targets = [c.kwargs['reply_to_message_id'] for c in calls]
    assert len(targets) > 3  # Actual Telegram message splitting, not just two updates.
    expected = [None] * len(targets) if mode == 'no' else [10] * len(targets)
    if mode == 'first':
        expected = [10] + [None] * (len(targets) - 1)
    assert targets == expected
    assert app.telegram.send_file.call_args.kwargs['reply_to_message_id'] == (10 if mode == 'all' else None)
    assert app.state.lookup_thread_for_message(-1001, 950) == 'task-a'
    # First resets for the next incoming request; a previous reply does not
    # disable quoting for the whole agent or group.
    thread, next_turn = await prepare(app, source=11, turn_id='next')
    assert await app._deliver_terminal_turn(thread, next_turn)
    assert app.telegram.send_message.call_args.kwargs['reply_to_message_id'] == (None if mode == 'no' else 11)


@pytest.mark.parametrize('mode', ['no', 'first', 'all'])
async def test_first_final_without_acknowledgment_uses_selected_mode(app, mode):
    app.routing.reply_mode = mode
    thread, turn = await prepare(app)
    assert await app._deliver_terminal_turn(thread, turn)
    assert app.telegram.send_message.call_args.kwargs['reply_to_message_id'] == (None if mode == 'no' else 10)


async def test_known_rejection_does_not_consume_first_reply(app):
    app.routing.reply_mode = 'first'
    thread, turn = await prepare(app)
    app.telegram.send_message.side_effect = [TelegramApiError('Retry later', retryable=True), SentMessage(-1001, 600, {})]
    assert not await app._deliver_terminal_turn(thread, turn)
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    thread.output_deliveries['output']['next_at'] = 0
    await app._drain_outputs(thread)
    assert [c.kwargs['reply_to_message_id'] for c in app.telegram.send_message.call_args_list] == [10, 10]
    assert thread.output_deliveries['output']['status'] == 'sent'


async def test_partial_split_retry_keeps_follow_up_plain_after_restart(app):
    app.routing.reply_mode = 'first'
    app.config.bridge.max_message_chars = 20
    thread, turn = await prepare(app, 'First paragraph.\n\nSecond paragraph.')
    app.telegram.send_message.side_effect = [SentMessage(-1001, 600, {}),
        TelegramApiError('Retry later', retryable=True), SentMessage(-1001, 601, {})]
    assert not await app._deliver_terminal_turn(thread, turn)
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']; thread.output_deliveries['output']['next_at'] = 0
    await app._drain_outputs(thread)
    assert [c.kwargs['reply_to_message_id'] for c in app.telegram.send_message.call_args_list] == [10, None, None]
    assert thread.output_deliveries['output']['sent_ids'] == [600, 601]


async def test_uncertain_first_send_is_not_quoted_or_replayed_again(app):
    app.routing.reply_mode = 'first'
    thread, turn = await prepare(app)
    app.telegram.send_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await app._deliver_terminal_turn(thread, turn)
    app.state = BridgeState.load(app.config.bridge.state_path)
    app.telegram.send_message.side_effect = lambda **kw: SentMessage(kw['chat_id'], 700, {})
    await update(app, 'c', 'A later update.')
    assert [c.kwargs['reply_to_message_id'] for c in app.telegram.send_message.call_args_list] == [10, None]
    assert app.state.threads['task-a'].output_deliveries['output']['status'] == 'uncertain'


async def test_first_backfills_older_saved_progress_receipts(app):
    app.routing.reply_mode = 'first'
    thread, turn = await prepare(app)
    thread.output_deliveries['old-progress'] = {'status': 'sent', 'reply_to': 10, 'sent_ids': [450]}
    assert not thread.reply_deliveries
    assert await app._deliver_terminal_turn(thread, turn)
    assert app.telegram.send_message.call_args.kwargs['reply_to_message_id'] is None


@pytest.mark.parametrize('mode', ['first', 'all'])
async def test_deleted_original_falls_back_to_plain_text_and_files(app, tmp_path, mode):
    app.routing.reply_mode = mode
    path = tmp_path / 'result.txt'; path.write_text('result')
    app.routing.file_roots['task-a'] = [tmp_path]
    thread, turn = await prepare(app, f'[Result](<{path}>)')
    app.telegram.send_file = AsyncMock(return_value=SentMessage(-1001, 800, {}))
    app.telegram.send_message.side_effect = [
        TelegramApiError('Bad Request: message to be replied not found', error_code=400),
        SentMessage(-1001, 700, {}),
    ]
    assert not await app._deliver_terminal_turn(thread, turn)
    thread.output_deliveries['output']['next_at'] = 0
    assert await app._deliver_terminal_turn(thread, turn)
    assert app.telegram.send_message.call_args.kwargs['reply_to_message_id'] is None
    assert app.telegram.send_file.call_args.kwargs['reply_to_message_id'] is None


async def test_plain_outputs_remain_replyable_for_agent_routing(app):
    app.routing.reply_mode = 'no'
    thread, turn = await prepare(app)
    assert await app._deliver_terminal_turn(thread, turn)
    sent_id = thread.last_chain_message_id
    app._send_thread_input = AsyncMock()
    await app._handle_telegram_message(message('Continue', message_id=11, reply=sent_id, reply_sender={'id': 9000, 'is_bot': True}))
    assert app._send_thread_input.call_args.args[0].thread_id == 'task-a'


async def test_owner_can_change_current_chat_mode_live_without_retargeting(app):
    path = app.config.bridge.state_path.parent / 'chat-routing.json'
    original = json.loads(path.read_text())
    await app._handle_telegram_message(message('/replymode First', user=42))
    raw = json.loads(path.read_text())
    assert raw == {**original, 'reply_modes': {'-1001': 'first'}}
    assert path.stat().st_mode & 0o777 == 0o600
    assert app.routing.reply_mode_for(-1001) == 'first'
    assert app.routing.reply_mode_for(-1002) == 'all'
    assert not app.desktop.sent_inputs
    app.telegram.send_message.assert_awaited_once()
    assert app.telegram.send_message.call_args.kwargs.get('reply_to_message_id') is None


async def test_collaborator_can_query_but_cannot_change_mode(app):
    path = app.config.bridge.state_path.parent / 'chat-routing.json'
    before = path.read_bytes()
    await app._handle_telegram_message(message('/replymode no'))
    assert 'Only the paired owner' in app.telegram.send_message.call_args.kwargs['text']
    assert path.read_bytes() == before
    await app._handle_telegram_message(message('/replymode'))
    assert 'Reply mode: All' in app.telegram.send_message.call_args.kwargs['text']
    assert not app.desktop.sent_inputs
