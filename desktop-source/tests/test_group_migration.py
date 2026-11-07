import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from codex_telegram_bridge.chat_routing import ChatRouting
from codex_telegram_bridge.group_migration import migration_plan, verify_migration
from codex_telegram_bridge.state import BridgeState, QueuedInput
from codex_telegram_bridge.telegram_api import TelegramApiError, TelegramBotApi
from test_chat_isolation import POLICY, app, message
from test_reply_modes import prepare


OLD, NEW = -1001, -1009


def saved():
    raw = copy.deepcopy(POLICY)
    raw['reply_modes'] = {str(OLD): 'no', '-1002': 'first', '42': 'all'}
    raw['agents']['editor_a']['file_roots'] = ['/approved/Editor A/Outgoing']
    state = BridgeState(telegram_update_offset=125, primary_chat_id=42)
    state.bind_message(OLD, 10, 'task-a')
    state.bind_message(-1002, 20, 'task-b')
    thread = state.threads['task-a']
    thread.input_receipts[f'{OLD}:10'] = {'status': 'confirmed', 'turn_id': 'old-turn'}
    thread.relay_turn_ids = ['old-turn']
    thread.pending_message_ids = [10]
    thread.output_deliveries = {
        'already-sent': {'status': 'sent', 'sent_ids': [11]},
        'pending': {'status': 'pending', 'sent_ids': [12], 'text_status': 'sending'},
        'unknown': {'status': 'uncertain', 'sent_ids': []},
        'failed': {'status': 'failed', 'sent_ids': []},
    }
    thread.reaction_deliveries[f'{OLD}:10'] = {'status': 'pending', 'stage': 'seen'}
    thread.last_delivered_turn_id = 'last-sent'
    return raw, state


def test_migration_preserves_grants_and_history_without_reusing_old_ids():
    raw, state = saved()
    original_raw, original_state = copy.deepcopy(raw), copy.deepcopy(state)
    changed, migrated, report = migration_plan(42, raw, state, OLD, NEW)
    assert raw == original_raw and state == original_state
    assert changed['agents'] == raw['agents']
    assert changed['groups'][str(NEW)] == raw['groups'][str(OLD)]
    assert changed['groups']['-1002'] == raw['groups']['-1002']
    policy = ChatRouting(42, changed)
    policy.validate_state(migrated)
    assert policy.reply_mode_for(NEW) == 'no'
    assert policy.reply_mode_for(-1002) == 'first'
    assert not policy.allowed(OLD, 'task-a')
    assert not policy.allowed(NEW, 'task-b')
    assert not policy.accepts_sender({'id': NEW, 'type': 'supergroup'}, {'id': 202})
    assert policy.accepts_sender({'id': NEW, 'type': 'supergroup'}, {'id': 101})
    assert migrated.lookup_thread_for_message(OLD, 10) is None
    assert migrated.lookup_thread_for_message(NEW, 10) is None
    assert report['archived_message_bindings'] == {f'{OLD}:10': 'task-a'}
    assert migrated.threads['task-a'].input_receipts == state.threads['task-a'].input_receipts
    assert migrated.threads['task-a'].relay_turn_ids == ['old-turn']
    assert migrated.threads['task-a'].last_delivered_turn_id == 'last-sent'
    assert migrated.threads['task-a'].last_chain_message_id is None
    assert migrated.threads['task-a'].pending_message_ids == []
    assert migrated.threads['task-b'] == state.threads['task-b']
    assert migrated.telegram_update_offset == 125
    for key in ('already-sent', 'unknown', 'failed'):
        assert migrated.threads['task-a'].output_deliveries[key] == state.threads['task-a'].output_deliveries[key]
    assert migrated.threads['task-a'].output_deliveries['pending']['status'] == 'blocked'
    assert migrated.threads['task-a'].output_deliveries['pending']['sent_ids'] == [12]


@pytest.mark.parametrize('old,new', [(42, NEW), (OLD, 42), (OLD, OLD), (True, NEW), (OLD, '-1009'), (-999, NEW), (OLD, -1002)])
def test_migration_fails_closed_for_invalid_ids_or_assigned_destinations(old, new):
    raw, state = saved()
    with pytest.raises(ValueError):
        migration_plan(42, raw, state, old, new)


def test_native_queued_input_is_not_rewritten_or_lost():
    raw, state = saved()
    state.threads['task-a'].queued_inputs = [QueuedInput(OLD, 10, 'exact saved native queue text', phase='submitted')]
    with pytest.raises(ValueError, match='queue'):
        migration_plan(42, raw, state, OLD, NEW)
    assert state.threads['task-a'].queued_inputs[0].text == 'exact saved native queue text'


def fake_api(error=None, *, kind='supergroup', status='administrator'):
    class Api:
        send_chat_action = AsyncMock(side_effect=error)
        get_me = AsyncMock(return_value={'id': 9000})
        _call = AsyncMock(side_effect=[{'id': NEW, 'type': kind, 'title': 'Same name proves nothing'}, {'status': status}])
    return Api()


async def test_fresh_telegram_migration_parameter_and_membership_are_required():
    api = fake_api(TelegramApiError('migrated', error_code=400, migrate_to_chat_id=NEW))
    proof = await verify_migration(api, OLD, NEW)
    assert proof['new_chat_id'] == NEW and proof['bot_id'] == 9000
    assert api._call.call_args_list[0].args == ('getChat', {'chat_id': NEW})


@pytest.mark.parametrize('error', [None, TelegramApiError('wrong destination', migrate_to_chat_id=-1008),
                                   TelegramApiError('unverified network result', uncertain=True, migrate_to_chat_id=NEW)])
async def test_titles_claims_and_uncertain_errors_cannot_authorize_migration(error):
    api = fake_api(error)
    with pytest.raises(ValueError, match='Telegram'):
        await verify_migration(api, OLD, NEW)
    api._call.assert_not_called()


@pytest.mark.parametrize('kind,status', [('group', 'administrator'), ('supergroup', 'left'), ('supergroup', 'kicked')])
async def test_destination_must_be_a_joined_supergroup(kind, status):
    with pytest.raises(ValueError, match='membership'):
        await verify_migration(fake_api(TelegramApiError('migrated', migrate_to_chat_id=NEW), kind=kind, status=status), OLD, NEW)


@pytest.mark.parametrize('value,expected', [(NEW, NEW), (True, None), (42, None), ('-1009', None), (None, None)])
async def test_api_preserves_only_numeric_negative_migration_parameter(value, expected):
    api = TelegramBotApi('test-token')
    response = httpx.Response(400, json={'ok': False, 'error_code': 400,
                                        'parameters': {'migrate_to_chat_id': value}, 'description': 'migrated'})
    with pytest.raises(TelegramApiError) as error:
        api._decode_response(response, 'sendChatAction')
    assert error.value.migrate_to_chat_id == expected
    await api.close()


async def test_new_group_accepts_its_members_and_never_drains_held_old_updates(app):
    thread, _ = await prepare(app)
    thread.output_deliveries['legacy'] = {'kind': 'progress', 'status': 'pending', 'text': 'Do not replay', 'reply_to': 10}
    raw = copy.deepcopy(POLICY)
    raw['reply_modes'] = {str(OLD): 'no'}
    changed, app.state, _ = migration_plan(42, raw, app.state, OLD, NEW)
    app.routing = ChatRouting(42, changed)
    await app._drain_outputs(app.state.threads['task-a'])
    app.telegram.send_message.assert_not_called()
    await app._handle_telegram_message(message('/ask must be ignored', chat=OLD))
    await app._handle_telegram_message(message('/ask wrong member', chat=NEW, user=202))
    assert not app.desktop.sent_inputs
    await app._handle_telegram_message(message('/ask new request', chat=NEW, user=101, message_id=1))
    assert len(app.desktop.sent_inputs) == 1
    assert f'Telegram chat ID: {NEW}' in app.desktop.sent_inputs[0][1]
    assert app.state.lookup_thread_for_message(NEW, 1) == 'task-a'


@pytest.mark.parametrize('fail_save', [False, True])
def test_admin_migration_keeps_backups_and_rolls_back_both_files_on_failure(tmp_path, monkeypatch, fail_save):
    import manage_chat_routes as admin
    from codex_telegram_bridge import chat_routing
    raw, state = saved()
    private = tmp_path / 'private'; private.mkdir()
    routing_path, state_path = private / 'chat-routing.json', private / 'state.json'
    routing_path.write_text(json.dumps(raw)); state.save(state_path)
    before = [routing_path.read_bytes(), state_path.read_bytes()]
    (private / 'config.toml').write_text('[telegram]\nbot_token="123456:fake"\nprimary_chat_id=42\nallowed_chat_ids=[42]\n')
    monkeypatch.setattr(admin, 'ROOT', tmp_path)
    monkeypatch.setattr(admin, 'verify_migration', AsyncMock(return_value={'old_chat_id': OLD, 'new_chat_id': NEW}))
    folder = private / 'outbox/requests'; folder.mkdir(parents=True)
    request = folder / ('a' * 64 + '.json')
    request.write_text(json.dumps({'thread_id': 'task-a', 'chat_id': OLD, 'reply_to': 10, 'text': 'held old update'}))
    if fail_save:
        def fail(*args):
            raise OSError('injected route write failure after state save')
        monkeypatch.setattr(chat_routing, 'save_routing', fail)
        with pytest.raises(OSError):
            admin.migrate_group(42, raw, state_path, routing_path, OLD, NEW)
        assert [routing_path.read_bytes(), state_path.read_bytes()] == before
        assert request.exists()
    else:
        admin.migrate_group(42, raw, state_path, routing_path, OLD, NEW)
        policy = ChatRouting.load(42, routing_path)
        policy.validate_state(BridgeState.load(state_path))
        assert not request.exists()
    backup = next((private / 'repair-evidence').iterdir())
    assert (backup / 'before-state.json').read_bytes() == before[1]
    receipt = json.loads((backup / 'receipt.json').read_text())
    assert receipt['status'] == ('rolled_back' if fail_save else 'complete')
