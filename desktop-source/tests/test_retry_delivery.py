import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from codex_telegram_bridge.current_desktop import matches_submitted_text
from codex_telegram_bridge.desktop_client import DesktopClientError, DesktopDraftConflictError, DesktopSendUnconfirmedError, DesktopRequest
from codex_telegram_bridge.formatting import render_markdown_chunks
from codex_telegram_bridge.local_mode import telegram_prompt
from codex_telegram_bridge.retry_delivery import retry_delay
from codex_telegram_bridge.state import BridgeState, QueuedInput
from codex_telegram_bridge.telegram_api import SentMessage, TelegramApiError, TelegramBotApi
from test_bridge import make_conversation, make_turn
from test_chat_isolation import app, message


def received(text, turn_id='received', status='inProgress'):
    return make_conversation('task-a', turns=[make_turn(turn_id, status, items=[{
        'type': 'userMessage', 'id': 'input', 'content': [{'type': 'text', 'text': text}],
    }])])


def test_backoff_is_exponential_capped_and_honors_retry_after():
    assert [retry_delay(i) for i in range(1, 10)] == [2, 4, 8, 16, 32, 60, 60, 60, 60]
    assert retry_delay(1, 45) == 45


async def test_startup_connection_retries_transient_errors_then_connects(app):
    bot = {'id':9000, 'username':'test_bot'}
    app._bot_id = None
    app.telegram.get_me = AsyncMock(side_effect=[
        TelegramApiError('Bad Gateway', retryable=True),
        TelegramApiError('Rate limited', retryable=True, retry_after=12), bot])
    app._wait_for_telegram_retry = AsyncMock(return_value=False)
    assert await app._connect_telegram() == bot
    assert [c.args[0] for c in app._wait_for_telegram_retry.call_args_list] == [2, 12]
    assert app.telegram.get_me.await_count == 3
    assert app._bot_id is None  # No guessed identity while connection is down.


async def test_startup_authentication_error_is_not_retried(app):
    app.telegram.get_me = AsyncMock(side_effect=TelegramApiError('Unauthorized', error_code=401))
    app._wait_for_telegram_retry = AsyncMock()
    with pytest.raises(TelegramApiError, match='Unauthorized'):
        await app._connect_telegram()
    app._wait_for_telegram_retry.assert_not_called()


async def test_stop_interrupts_startup_backoff(app):
    async def failure():
        await app.stop()
        raise TelegramApiError('Bad Gateway', retryable=True, retry_after=60)
    app.telegram.get_me = AsyncMock(side_effect=failure)
    async with asyncio.timeout(.5):
        assert await app._connect_telegram() is None
    app.telegram.get_me.assert_awaited_once()


def test_acknowledgment_decodes_html_entities_without_losing_literal_entities():
    expected = 'Telegram message ID: 115\nMessage:\nThis group \nCodify this & keep &#x20; literal'
    saved = 'Telegram message ID: 115\nMessage:\nThis group&#x20;\nCodify this &amp; keep &amp;#x20; literal'
    turn = received(saved).latest_turn
    assert matches_submitted_text(turn, expected)
    assert not matches_submitted_text(turn, expected.replace('115', '117'))
    assert not matches_submitted_text(turn, expected.replace('&#x20;', ' '))


async def test_delayed_acknowledgment_reconciles_after_restart_without_second_click(app):
    incoming = message('/ask Codify this')
    prompt = app._telegram_prompt(incoming, 'Codify this', 'task-a')
    async def send(task, text, *, checkpoint, **kwargs):
        await checkpoint('verify')
        saved = BridgeState.load(app.config.bridge.state_path)
        assert saved.threads[task].queued_inputs[0].phase == 'verify'
        raise DesktopSendUnconfirmedError(thread_id=task, expected_text=text, after_turn_count=1)
    app.desktop.send_message = AsyncMock(side_effect=send)
    await app._handle_telegram_message(incoming)
    app.telegram.send_message.assert_not_called()
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    thread.queued_inputs[0].next_attempt_at = 0
    app.desktop.threads['task-a'] = received(prompt)
    await app._attempt_queued_input(thread)
    assert not thread.queued_inputs and thread.input_receipts['-1001:10']['turn_id'] == 'received'
    assert 'received' in thread.relay_turn_ids
    await app._handle_telegram_message(incoming)  # Replayed Telegram update.
    app.desktop.send_message.assert_awaited_once()


async def test_safe_failure_retries_with_saved_draft_and_does_not_wait_on_desktop(app, monkeypatch):
    monkeypatch.setattr('codex_telegram_bridge.retry_delivery.time.time', lambda: 100)
    app.desktop.send_message = AsyncMock(side_effect=DesktopClientError('Send not ready'))
    incoming = message('/ask Continue')
    await app._handle_telegram_message(incoming)
    thread = app.state.threads['task-a']
    assert thread.queued_inputs[0].next_attempt_at == 102
    await app._attempt_queued_input(thread)
    app.desktop.send_message.assert_awaited_once()
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']; thread.queued_inputs[0].next_attempt_at = 0
    async def success(task, text, *, recover_draft, checkpoint):
        assert recover_draft
        await checkpoint('verify')
        result = received(text)
        app.desktop.threads[task] = result
        return result
    app.desktop.send_message.side_effect = success
    await app._attempt_queued_input(thread)
    assert not thread.queued_inputs and app.desktop.send_message.await_count == 2


async def test_unsent_draft_waits_without_failure_notice_and_recovers_after_restart(app, monkeypatch, caplog):
    monkeypatch.setattr('codex_telegram_bridge.retry_delivery.time.time', lambda: 100)
    app.desktop.send_message = AsyncMock(side_effect=DesktopDraftConflictError(
        context='current task', draft_text='Private unsent draft'))
    await app._handle_telegram_message(message('/ask Hello'))
    thread = app.state.threads['task-a']
    job = thread.queued_inputs[0]
    original_text = job.text
    assert job.phase == 'awaiting_draft' and job.attempts == 0 and job.next_attempt_at == 110
    for _ in range(10):
        job.next_attempt_at = 0
        await app._attempt_queued_input(thread)
    assert job.text == original_text and job.attempts == 0 and not job.notice_sent
    app.telegram.send_message.assert_not_called()
    assert 'Private unsent draft' not in caplog.text
    assert caplog.text.count('desktop draft must be resolved') == 1
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    thread.queued_inputs[0].next_attempt_at = 0
    async def success(task, text, *, recover_draft, **kwargs):
        assert recover_draft and text == original_text
        result = received(text)
        app.desktop.threads[task] = result
        return result
    app.desktop.send_message.side_effect = success
    await app._attempt_queued_input(thread)
    assert not thread.queued_inputs and thread.input_receipts['-1001:10']['status'] == 'confirmed'


async def test_cancelled_click_is_persisted_and_only_checks_confirmation(app):
    async def cancelled(task, text, *, checkpoint, **kwargs):
        await checkpoint('verify')
        raise asyncio.CancelledError()
    app.desktop.send_message = AsyncMock(side_effect=cancelled)
    with pytest.raises(asyncio.CancelledError):
        await app._handle_telegram_message(message('/ask Continue'))
    assert not app._threads_with_send_in_flight
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    await app._attempt_queued_input(thread)
    assert thread.queued_inputs[0].phase == 'verify'
    app.desktop.send_message.assert_awaited_once()


async def test_busy_agent_queues_without_consuming_retry_attempts(app):
    await app._attach_group(-1001, 'task-a')
    app.desktop.threads['task-a'] = received('Existing task')
    app.desktop.send_message = AsyncMock()
    await app._send_thread_input(app.state.threads['task-a'], text='New instruction', source_chat_id=-1001, source_message_id=10)
    job = app.state.threads['task-a'].queued_inputs[0]
    assert job.attempts == 0 and job.phase == 'ready'
    app.desktop.send_message.assert_not_called()


async def test_failed_output_does_not_block_next_input_and_receipt_survives(app):
    thread, turn = await prepare_output(app, 'Previous result')
    app.desktop.threads['task-a'] = make_conversation('task-a', turns=[turn])
    app.telegram.send_message.side_effect = TelegramApiError('Service unavailable', retryable=True, error_code=503)
    assert not await app._deliver_terminal_turn(thread, turn)
    text = telegram_prompt(message('/ask Continue', message_id=11), 'Continue')
    async def send(task, prompt, **kwargs):
        result = received(prompt, turn_id='next')
        app.desktop.threads[task] = result
        return result
    app.desktop.send_message = AsyncMock(side_effect=send)
    thread.queued_inputs.append(QueuedInput(-1001, 11, text))
    await app._sync_thread('task-a')
    assert thread.current_turn_id == 'next' and not thread.queued_inputs
    assert thread.output_deliveries['output']['status'] == 'pending'
    assert thread.input_receipts['-1001:11']['turn_id'] == 'next'
    app.desktop.send_message.assert_awaited_once()


async def prepare_output(app, text='First paragraph.\n\nSecond paragraph.'):
    await app._attach_group(-1001, 'task-a')
    thread = app.state.threads['task-a']; thread.relay_turn_ids = ['output']
    turn = make_turn('output', 'completed', items=[{'type': 'agentMessage', 'id': 'answer', 'text': text}])
    return thread, turn


async def test_partial_reply_resumes_after_backoff_and_restart_without_duplicates(app, monkeypatch):
    monkeypatch.setattr('codex_telegram_bridge.retry_delivery.time.time', lambda: 100)
    app.config.bridge.max_message_chars = 20
    thread, turn = await prepare_output(app)
    app.telegram.send_message.side_effect = [SentMessage(-1001, 501, {}),
        TelegramApiError('Rate limited', retryable=True, retry_after=20, error_code=429), SentMessage(-1001, 502, {})]
    assert not await app._deliver_terminal_turn(thread, turn)
    entry = thread.output_deliveries['output']
    assert entry['sent_ids'] == [501] and entry['next_at'] == 120
    assert not await app._deliver_terminal_turn(thread, turn)
    assert app.telegram.send_message.await_count == 2
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']; thread.output_deliveries['output']['next_at'] = 0
    await app._drain_outputs(thread)
    assert thread.output_deliveries['output']['status'] == 'sent'
    assert thread.output_deliveries['output']['sent_ids'] == [501, 502]
    calls = app.telegram.send_message.call_args_list
    assert calls[0].kwargs['text'] != calls[1].kwargs['text'] == calls[2].kwargs['text']
    assert app.state.lookup_thread_for_message(-1001, 502) == 'task-a'


async def test_ambiguous_outbound_timeout_is_not_replayed(app):
    thread, turn = await prepare_output(app, 'One reply')
    app.telegram.send_message.side_effect = TelegramApiError('Read timeout', uncertain=True)
    assert not await app._deliver_terminal_turn(thread, turn)
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    await app._drain_outputs(thread)
    assert thread.output_deliveries['output']['status'] == 'uncertain'
    app.telegram.send_message.assert_awaited_once()


async def test_outbound_process_interruption_does_not_replay_unconfirmed_part(app):
    thread, turn = await prepare_output(app)
    entry = app._stage_terminal_output(thread, turn)
    entry['text_status'] = 'sending'
    app.state.save(app.config.bridge.state_path)
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    await app._drain_outputs(thread)
    app.telegram.send_message.assert_not_called()
    assert thread.output_deliveries['output']['status'] == 'uncertain'


async def test_outbound_permanent_error_stops_hot_polling(app):
    thread, turn = await prepare_output(app, 'One reply')
    app.telegram.send_message.side_effect = TelegramApiError('Forbidden', error_code=403)
    assert not await app._deliver_terminal_turn(thread, turn)
    for _ in range(5):
        await app._drain_outputs(thread)
    app.telegram.send_message.assert_awaited_once()
    assert thread.output_deliveries['output']['status'] == 'failed'


@pytest.mark.parametrize('target', [
    '</Users/example/Project D/R04 Finish/master.mp4>', '/Users/example/Project%20D/master.mp4',
    '<file:///Users/example/Project D/master.mp4>', '</Users/example/Exports/cut (2).mp4>',
])
def test_local_artifact_links_never_become_telegram_url_entities(target):
    chunks = render_markdown_chunks(f'[Master]({target}) and [Website](https://example.com)', 3900)
    links = [e['url'] for c in chunks for e in c.entities if e['type'] == 'text_link']
    assert links == ['https://example.com']
    assert 'Master' in chunks[0].text


@pytest.mark.parametrize('error,safe,uncertain', [
    (httpx.ConnectTimeout('connect'), True, False), (httpx.ReadTimeout('read'), False, True),
])
async def test_transport_failures_distinguish_safe_retries(error, safe, uncertain):
    api = TelegramBotApi('123:fake')
    api._client.post = AsyncMock(side_effect=error)
    try:
        with pytest.raises(TelegramApiError) as caught:
            await api.send_message(chat_id=42, text='test')
        assert caught.value.retryable is safe and caught.value.uncertain is uncertain
    finally:
        await api.close()


async def test_progress_outbox_is_scoped_and_repeated_request_is_idempotent(app):
    app.state.bind_message(-1001, 10, 'task-a')
    folder = app.config.bridge.state_path.parent / 'outbox/requests'; folder.mkdir(parents=True)
    path = folder / ('a' * 64 + '.json')
    request = {'thread_id': 'task-a', 'chat_id': -1001, 'reply_to': 10, 'text': 'Working on the revision.'}
    path.write_text(json.dumps(request))
    await app._sync_all_threads_once()
    assert app.telegram.send_message.await_count == 1
    path.write_text(json.dumps(request))
    await app._sync_all_threads_once()
    assert app.telegram.send_message.await_count == 1
    receipt = json.loads((folder.parent / 'receipts' / path.name).read_text())
    assert receipt['status'] == 'sent' and receipt['sent_ids']
    path = folder / ('b' * 64 + '.json'); request['chat_id'] = -1002
    path.write_text(json.dumps(request))
    await app._sync_all_threads_once()
    assert app.telegram.send_message.await_count == 1 and path.with_suffix('.rejected').exists()


async def test_question_outbox_delivers_while_desktop_question_is_pending(app):
    app.state.bind_message(-1001, 10, 'task-a')
    thread = app.state.threads['task-a']
    thread.primary_chat_id = -1001
    thread.relay_turn_ids = ['active-turn']
    conversation = received('Working on project',turn_id='active-turn')
    conversation.requests = [DesktopRequest('question-1','user_input',{'reason':'Choose an export folder'})]
    app.desktop.threads['task-a'] = conversation
    folder = app.config.bridge.state_path.parent / 'outbox/requests'; folder.mkdir(parents=True)
    path = folder / ('c'*64+'.json')
    question = 'May I upload the review files to this group?'
    path.write_text(json.dumps({'thread_id':'task-a','chat_id':-1001,'reply_to':10,'text':question}))
    await app._sync_all_threads_once()
    receipt = json.loads((folder.parent/'receipts'/path.name).read_text())
    assert receipt['status'] == 'sent' and receipt['sent_ids']
    assert any(c.kwargs['text'] == question for c in app.telegram.send_message.call_args_list)
    assert app.desktop.threads['task-a'].requests[0].request_id == 'question-1'
    assert not app.desktop.approval_clicks
