import asyncio
import json
import shutil
import subprocess
from unittest.mock import AsyncMock, Mock

import pytest

from codex_telegram_bridge.current_desktop import CurrentDesktopClient
from codex_telegram_bridge.desktop_client import DesktopBusyError, DesktopQueueModeError, DesktopQueuedReceipt, DesktopSendUnconfirmedError
from codex_telegram_bridge.local_mode import SharedDesktopClient
from codex_telegram_bridge.state import BridgeState, QueuedInput
from test_bridge import make_conversation, make_turn
from test_chat_isolation import app, message
from test_current_desktop import TASK, append, event, idle_client, log
from test_retry_delivery import received


async def test_busy_followups_enter_native_queue_in_order_and_survive_restart(app):
    app.desktop.supports_native_queue = True
    app.desktop.threads['task-a'] = received('Long-running goal')
    async def queue(task, text, *, queue_if_busy, checkpoint, **kwargs):
        assert queue_if_busy
        await checkpoint('verify')
        return DesktopQueuedReceipt('native-' + str(app.desktop.send_message.await_count))
    app.desktop.send_message = AsyncMock(side_effect=queue)
    await app._handle_telegram_message(message('/ask First correction', message_id=10))
    await app._handle_telegram_message(message('/ask Second correction', message_id=11))
    thread = app.state.threads['task-a']
    assert [p.phase for p in thread.queued_inputs] == ['native_queued'] * 2
    assert [p.native_queue_id for p in thread.queued_inputs] == ['native-1', 'native-2']
    assert thread.input_receipts['-1001:10'] == {'status': 'queued', 'queue_id': 'native-1'}
    assert thread.current_turn_id == 'received'
    assert not thread.relay_turn_ids  # Queuing doesn't attribute the active desktop turn to Telegram.
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    await app._handle_telegram_message(message('/ask Second correction', message_id=11))
    await app._attempt_queued_input(thread)
    assert app.desktop.send_message.await_count == 2
    app.telegram.send_message.assert_not_called()

    # Both messages can finish between polls. Reconcile each and return each
    # result to its own Telegram request, without exporting the earlier goal.
    turns = [make_turn('private', 'completed', items=[{'type': 'agentMessage', 'text': 'PRIVATE'}])]
    for p in thread.queued_inputs:
        turn = received(p.text, turn_id=f'turn-{p.message_id}', status='completed').latest_turn
        turn.items.append({'type': 'agentMessage', 'id': f'a-{p.message_id}', 'text': f'Answer {p.message_id}'})
        turn.raw['items'] = turn.items
        turns.append(turn)
    app.desktop.threads['task-a'] = make_conversation('task-a', turns=turns)
    await app._sync_thread('task-a')
    assert not thread.queued_inputs
    assert all(r['status'] == 'confirmed' for r in thread.input_receipts.values())
    sent = [c.kwargs for c in app.telegram.send_message.call_args_list]
    assert [(c['text'], c['reply_to_message_id']) for c in sent] == [('Answer 10', 10), ('Answer 11', 11)]
    assert app.desktop.send_message.await_count == 2


async def test_unknown_queue_click_reconciles_after_restart_without_resubmission(app):
    app.desktop.supports_native_queue = True
    app.desktop.threads['task-a'] = received('Running')
    native = []
    async def uncertain(task, text, *, checkpoint, **kwargs):
        await checkpoint('verify')
        native.append({'id': 'queued-1', 'text': text})
        raise asyncio.CancelledError()
    app.desktop.send_message = AsyncMock(side_effect=uncertain)
    with pytest.raises(asyncio.CancelledError):
        await app._handle_telegram_message(message('/ask Fix direction'))
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    app.desktop.read_native_queue = AsyncMock(return_value=native)
    await app._attempt_queued_input(thread)
    assert thread.queued_inputs[0].phase == 'native_queued'
    assert thread.queued_inputs[0].native_queue_id == 'queued-1'
    app.desktop.send_message.assert_awaited_once()


async def test_unconfirmed_queue_entry_blocks_later_message_and_is_never_reclicked(app):
    app.desktop.supports_native_queue = True
    app.desktop.threads['task-a'] = received('Running')
    async def uncertain(task, text, *, checkpoint, **kwargs):
        await checkpoint('verify')
        raise DesktopSendUnconfirmedError(thread_id=task, expected_text=text, after_turn_count=1)
    app.desktop.send_message = AsyncMock(side_effect=uncertain)
    app.desktop.read_native_queue = AsyncMock(return_value=[])
    await app._handle_telegram_message(message('/ask First', message_id=10))
    await app._handle_telegram_message(message('/ask Second', message_id=11))
    thread = app.state.threads['task-a']
    thread.queued_inputs[0].next_attempt_at = 0
    await app._attempt_queued_input(thread)
    assert [p.phase for p in thread.queued_inputs] == ['verify', 'ready']
    app.desktop.send_message.assert_awaited_once()


@pytest.mark.parametrize('ambient', [False, True])
async def test_formatted_brief_reconciles_after_restart_and_unblocks_followup(app, ambient):
    app.desktop.supports_native_queue = True
    await app._attach_group(-1001, 'task-a')
    brief = app._telegram_prompt(message('/ask Brief', message_id=12),
        'Footage: https://example.com/a_b?key=1&dl=0\n1. Review footage', 'task-a')
    followup = app._telegram_prompt(message('/ask Seen?', message_id=19, user=444), 'Seen?', 'task-a')
    thread = app.state.threads['task-a']
    thread.queued_inputs = [QueuedInput(-1001, 12, brief), QueuedInput(-1001, 19, followup)]
    thread.queued_inputs[0].phase = 'verify'
    thread.queued_inputs[0].attempts = 7
    thread.queued_inputs[0].baseline_turn_ids = ['old-a']
    thread.pending_message_ids = [12, 19]
    await app._save_state()
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    serialized = brief.replace('https://example.com/a_b?key=1&dl=0',
        r'[https://example.com/a_b?key=1&dl=0](https://example.com/a_b?key=1\&dl=0)').replace('1. Review', r'1\. Review')
    if ambient:
        serialized = '<in-app-browser-context source="ambient-ui-state">\nCurrent URL: https://example.com\n</in-app-browser-context>\n\n## My request:\n' + serialized
    app.desktop.threads['task-a'] = received(serialized, turn_id='brief-turn')
    app.desktop.send_message = AsyncMock(return_value=DesktopQueuedReceipt('followup-queue'))
    app.telegram.send_message.reset_mock()
    await app._attempt_queued_input(thread)
    assert thread.input_receipts['-1001:12'] == {'status':'confirmed','turn_id':'brief-turn'}
    assert thread.reaction_deliveries['-1001:12']['stage'] == 'seen'
    assert [(p.message_id, p.phase) for p in thread.queued_inputs] == [(19, 'native_queued')]
    assert app.desktop.send_message.await_count == 1
    assert app.desktop.send_message.call_args.args == ('task-a', followup)
    app.telegram.send_message.assert_not_called()
    await app._attempt_queued_input(thread)
    app.desktop.send_message.assert_awaited_once()


async def test_desktop_booking_wait_does_not_consume_attempts_or_block_receiving(app):
    app.desktop.supports_native_queue = True
    app.desktop.threads['task-a'] = received('Running')
    app.desktop.send_message = AsyncMock(side_effect=DesktopBusyError('Reserved'))
    async with asyncio.timeout(1):
        await app._handle_telegram_message(message('/ask First', message_id=10))
        await app._handle_telegram_message(message('/ask Second', message_id=11))
    pending = app.state.threads['task-a'].queued_inputs
    assert len(pending) == 2
    assert all(p.attempts == 0 and p.phase == 'ready' for p in pending)
    app.telegram.send_message.assert_not_called()


async def test_steer_setting_waits_quietly_and_recovers_exact_draft_when_queue_enabled(app):
    app.desktop.supports_native_queue = True
    app.desktop.threads['task-a'] = received('Running')
    app.desktop.send_message = AsyncMock(side_effect=DesktopQueueModeError('Choose Queue'))
    await app._handle_telegram_message(message('/ask Follow up'))
    thread = app.state.threads['task-a']
    assert thread.queued_inputs[0].phase == 'awaiting_queue_mode'
    assert thread.queued_inputs[0].attempts == 0
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads['task-a']
    for _ in range(10):
        thread.queued_inputs[0].next_attempt_at = 0
        await app._attempt_queued_input(thread)
        assert thread.queued_inputs[0].attempts == 0
        assert app.desktop.send_message.call_args.kwargs['recover_draft']
    app.telegram.send_message.assert_not_called()
    app.desktop.send_message.side_effect = None
    app.desktop.send_message.return_value = DesktopQueuedReceipt('q1')
    thread.queued_inputs[0].next_attempt_at = 0
    await app._attempt_queued_input(thread)
    assert thread.queued_inputs[0].phase == 'native_queued'
    assert app.desktop.send_message.call_args.kwargs['recover_draft']


async def test_shared_desktop_reservation_returns_promptly_and_cleans_up_on_close(tmp_path):
    client = SharedDesktopClient(task_titles={TASK: 'Editor A'}, allowed_thread_ids={TASK},
        app_path=tmp_path, remote_debugging_port=9229, user_data_dir=tmp_path,
        launch_timeout_seconds=1, send_ack_timeout_seconds=1, poll_interval_seconds=.01)
    client.booking = Mock()
    client.booking.try_claim.return_value = False
    try:
        async with asyncio.timeout(.2):
            with pytest.raises(DesktopBusyError):
                async with client.ui():
                    pytest.fail('Desktop must not be used while another agent owns it')
        client.booking.release.assert_not_called()
        assert client._waiting_for_desktop
    finally:
        await client.close()
    client.booking.cancel_reservation.assert_called_once()


async def test_submit_returns_native_receipt_while_original_turn_is_still_active(idle_client):
    client, path = idle_client
    append(path, event('task_started', 'busy'))
    client._eval_json = AsyncMock(return_value={'ok': True, 'mode': 'Queue'})
    client._visible_native_queue = AsyncMock(return_value=[{'id': 'q1', 'text': 'follow-up'}])
    checkpoint = AsyncMock()
    result = await client._submit_and_wait(TASK, 'follow-up', {'turn-1', 'busy'},
                                           checkpoint=checkpoint, queue_if_busy=True)
    assert result == DesktopQueuedReceipt('q1')
    client._eval_json.assert_awaited_once()
    checkpoint.assert_awaited_once_with('verify')


async def test_native_queue_receipt_requires_exact_text_and_single_match(idle_client):
    client, _ = idle_client
    assert client.queued_receipt([{'id': 'other', 'text': 'wrong request'}], 'follow-up') is None
    from codex_telegram_bridge.desktop_client import DesktopClientError
    with pytest.raises(DesktopClientError, match='Multiple native queue'):
        client.queued_receipt([{'id': 'a', 'text': 'same'}, {'id': 'b', 'text': 'same'}], 'same')


async def test_queue_mode_checks_native_receipt_before_inserting_a_retry(idle_client):
    client, _ = idle_client
    client.activate_thread = AsyncMock()
    client._visible_native_queue = AsyncMock(return_value=[{'id': 'existing', 'text': 'follow-up'}])
    client._call_cdp = AsyncMock()
    assert await client.send_message(TASK, 'follow-up', queue_if_busy=True) == DesktopQueuedReceipt('existing')
    client._call_cdp.assert_not_called()


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is needed for the actual DOM guard script')
@pytest.mark.parametrize('labels,text,active,expected,clicks', [
    (['Queue'], 'follow-up', TASK, None, ['Queue']),
    (['Steer', 'Queue'], 'follow-up', TASK, None, ['Queue']),  # An earlier queued row has its own Steer action.
    (['Send'], 'follow-up', TASK, None, ['Send']),
    (['Steer'], 'follow-up', TASK, 'choose-queue-follow-up-behavior-in-app-settings', []),
    (['Stop'], 'follow-up', TASK, 'send-not-ready', []),
    (['Queue', 'Queue'], 'follow-up', TASK, 'send-not-ready', []),
    (['Queue'], 'another draft', TASK, 'composer-updating', []),
    (['Queue'], 'follow-up', 'foreign-task', 'wrong-task', []),
])
async def test_actual_submission_script_never_steers_or_sends_wrong_draft(
        idle_client, labels, text, active, expected, clicks):
    from codex_telegram_bridge.desktop_client import DesktopClientError
    client, _ = idle_client
    client._eval_json = AsyncMock(return_value={'ok': False, 'error': 'wrong-task'})
    with pytest.raises(DesktopClientError):
        await client._submit_and_wait(TASK, 'follow-up', {'turn-1'}, queue_if_busy=True)
    expression = client._eval_json.call_args.args[0]
    fixture = r'''
const input=JSON.parse(require('fs').readFileSync(0,'utf8')), clicks=[];
const buttons=input.labels.map(label=>({disabled:false,getAttribute:()=>label,click:()=>clicks.push(label)}));
const composer={innerText:input.text+'\nSuggested reply\nPress Tab to accept', getBoundingClientRect:()=>({width:200,height:50}),
  pmViewDesc:{node:{type:{name:'doc'},content:{size:input.text.length},textBetween:()=>input.text}},
  closest:()=>({querySelectorAll:()=>buttons})};
const root={__reactContainerTest:{stateNode:{current:{memoizedProps:{activeThreadId:input.active}}}}};
global.getComputedStyle=()=>({visibility:'visible',display:'block'});
global.document={getElementById:()=>root,querySelectorAll:s=>s.includes('contenteditable')?[composer]:[]};
const result=eval(input.expression);
process.stdout.write(JSON.stringify({result,clicks}));
'''
    run = subprocess.run([shutil.which('node'), '-e', fixture], text=True, capture_output=True,
        input=json.dumps(dict(labels=labels, text=text, active=active, expression=expression)), check=True)
    outcome = json.loads(run.stdout)
    assert outcome['clicks'] == clicks
    assert outcome['result'].get('error') == expected
