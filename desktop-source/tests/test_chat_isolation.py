import copy
import json
from unittest.mock import AsyncMock

import pytest

from codex_telegram_bridge.chat_routing import ChatRouting
from codex_telegram_bridge.config import AppConfig, BridgeConfig, DesktopConfig, TelegramConfig
from codex_telegram_bridge.local_mode import LocalBridgeApp, telegram_prompt
from codex_telegram_bridge.desktop_client import DesktopClientError
from codex_telegram_bridge.state import BridgeState, QueuedInput
from codex_telegram_bridge.telegram_api import SentMessage
from test_bridge import FakeDesktop, make_conversation, make_turn


POLICY = {"version": 1, "default_private_agent": "personal", "agents": {
    "editor_a": {"thread_id": "task-a", "title": "Editor A"},
    "editor_b": {"thread_id": "task-b", "title": "Editor B"},
    "personal": {"thread_id": "task-personal", "title": "Personal"},
}, "groups": {
    "-1001": {"agent": "editor_a", "allowed_user_ids": [101]},
    "-1002": {"agent": "editor_b", "allowed_user_ids": [202]},
}}


def message(text, *, chat=-1001, user=101, message_id=10, reply=None, reply_sender=None):
    result = {"message_id": message_id, "text": text, "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"}, "from": {"id": user}}
    if reply is not None:
        result["reply_to_message"] = {"message_id": reply}
        if reply_sender is not None:
            result["reply_to_message"]["from"] = reply_sender
    return result


@pytest.fixture
async def app(tmp_path):
    (tmp_path / "chat-routing.json").write_text(json.dumps(POLICY))
    config = AppConfig(TelegramConfig("123456:fake", primary_chat_id=42, allowed_chat_ids=[42]), DesktopConfig(),
                       BridgeConfig(state_path=tmp_path / "state.json", allow_first_private_chat=False))
    result = LocalBridgeApp(config, BridgeState())
    await result.desktop.close()
    result.desktop = FakeDesktop(threads={
        "task-a": make_conversation("task-a", turns=[make_turn("old-a", "completed", items=[{"id": "secret-a", "type": "agentMessage", "text": "OLD PRIVATE A"}])]),
        "task-b": make_conversation("task-b", turns=[make_turn("old-b", "completed", items=[{"id": "secret-b", "type": "agentMessage", "text": "OLD PRIVATE B"}])]),
    })
    result._bot_username = "test_bot"
    result._bot_id = 9000
    # Existing routing tests drain submissions deterministically. Dedicated
    # concurrency tests exercise LocalBridgeApp's production dispatcher.
    result._dispatch_queued_input = result._attempt_queued_input
    counter = 500
    async def send(**kwargs):
        nonlocal counter
        counter += 1
        return SentMessage(kwargs["chat_id"], counter, {})
    result.telegram.send_message = AsyncMock(side_effect=send)
    result.telegram.set_message_reaction = AsyncMock(return_value=True)
    result.telegram.answer_callback_query = AsyncMock()
    yield result
    await result.close()


def test_policy_rejects_duplicate_group_task():
    raw = copy.deepcopy(POLICY)
    raw["groups"]["-1002"]["agent"] = "editor_a"
    with pytest.raises(ValueError, match="more than one group"):
        ChatRouting(42, raw)


@pytest.mark.parametrize("key,value", [("allowed_user_ids", [-1]), ("all_members", "false"), ("agent", "missing")])
def test_invalid_group_permissions_fail_closed(key, value):
    raw = copy.deepcopy(POLICY)
    raw["groups"]["-1001"][key] = value
    with pytest.raises(ValueError):
        ChatRouting(42, raw)


@pytest.mark.parametrize('value', ['false', 'true', 0, 1, None, []])
def test_invalid_reply_chain_option_rejected(value):
    raw = copy.deepcopy(POLICY)
    raw['groups']['-1001']['follow_reply_chains'] = value
    with pytest.raises(ValueError, match='follow_reply_chains'):
        ChatRouting(42, raw)


def test_existing_groups_default_to_not_following_human_replies():
    assert ChatRouting(42, POLICY).groups[-1001]['follow_reply_chains'] is False


def test_saved_foreign_chat_bindings_and_queue_rejected():
    policy = ChatRouting(42, POLICY)
    state = BridgeState()
    state.bind_message(-1002, 10, "task-a")
    with pytest.raises(ValueError, match="binding"):
        policy.validate_state(state)
    state.threads["task-a"].primary_chat_id = -1001
    state.message_bindings.clear()
    state.threads["task-a"].queued_inputs.append(QueuedInput(-1002, 10, "cross-group"))
    with pytest.raises(ValueError, match="queue"):
        policy.validate_state(state)


@pytest.mark.parametrize("chat,user", [(-9999, 101), (-1001, 202), (-1001, 999), (202, 202)])
@pytest.mark.parametrize("text", ["/ask Do work", "Do work"])
async def test_unconfigured_chats_and_unauthorized_senders_ignored(app, chat, user, text):
    await app._handle_telegram_message(message(text, chat=chat, user=user))
    assert not app.desktop.sent_inputs
    app.telegram.send_message.assert_not_called()


async def test_each_group_sees_only_its_agent(app):
    await app._handle_telegram_message(message("/agents"))
    payload = app.telegram.send_message.call_args.kwargs["text"]
    assert "Editor A" in payload and "Editor B" not in payload and "Personal" not in payload


@pytest.mark.parametrize("text", ["/editor_b steal", "/attach task-b", "/attach editor_b", "/detach task-b", "/personal secret", "/new task-b"])
async def test_cross_agent_commands_rejected_without_state_mutation(app, text):
    await app._handle_telegram_message(message(text))
    assert not app.desktop.sent_inputs
    assert not app.state.threads
    assert "not available" in app.telegram.send_message.call_args.kwargs["text"]


async def test_private_owner_cannot_retarget_group_session(app):
    await app._handle_telegram_message(message("/editor_a Private message", chat=42, user=42))
    assert not app.desktop.sent_inputs
    assert not app.state.threads


async def test_attach_and_poll_do_not_export_historical_private_content(app):
    await app._handle_telegram_message(message("/attach"))
    await app._sync_thread("task-a")
    content = " ".join(call.kwargs["text"] for call in app.telegram.send_message.call_args_list)
    assert "OLD PRIVATE" not in content
    assert set(app.state.threads) == {"task-a"}
    assert app.state.threads["task-a"].primary_chat_id == -1001


async def test_addressed_group_command_sends_and_routes_completion(app):
    turn = make_turn("new-a", "completed", items=[{"id": "reply-a", "type": "agentMessage", "text": "A COMPLETED"}])
    incoming = message("/ask@test_bot Edit my clip")
    prompt = app._telegram_prompt(incoming, "Edit my clip", "task-a")
    turn.items.insert(0, {'type': 'userMessage', 'id': 'input-a', 'content': [{'type': 'text', 'text': prompt}]})
    app.desktop.send_results[("task-a", prompt)] = make_conversation("task-a", turns=[turn])
    await app._handle_telegram_message(incoming)
    assert app.desktop.sent_inputs == [("task-a", prompt)]
    thread = app.state.threads["task-a"]
    assert "new-a" in thread.relay_turn_ids
    app.desktop.threads["task-a"] = make_conversation("task-a", turns=[turn])
    await app._sync_thread("task-a")
    outputs = [call.kwargs for call in app.telegram.send_message.call_args_list]
    assert any("A COMPLETED" in out["text"] for out in outputs)
    assert all(out["chat_id"] == -1001 for out in outputs)
    saved = BridgeState.load(app.config.bridge.state_path)
    assert saved.threads["task-a"].relay_turn_ids == ["new-a"]


async def test_command_for_another_bot_ignored(app):
    await app._handle_telegram_message(message("/ask@other_bot Do work"))
    assert not app.desktop.sent_inputs
    app.telegram.send_message.assert_not_called()


async def test_same_message_numbers_in_two_groups_do_not_cross_route(app):
    app.state.bind_message(-1001, 50, "task-a")
    app.state.bind_message(-1002, 50, "task-b")
    app._send_thread_input = AsyncMock()
    await app._handle_telegram_message(message("Continue", reply=50, reply_sender={'id': 9000, 'is_bot': True}))
    assert app._send_thread_input.call_args.args[0].thread_id == "task-a"
    await app._handle_telegram_message(message("Continue", chat=-1002, user=202, reply=50, reply_sender={'id': 9000, 'is_bot': True}))
    assert app._send_thread_input.call_args.args[0].thread_id == "task-b"


async def test_forged_foreign_binding_cannot_send_or_receive(app):
    app.state.bind_message(-1001, 50, "task-b")
    await app._handle_telegram_message(message("Continue", reply=50, reply_sender={'id': 9000, 'is_bot': True}))
    assert not app.desktop.sent_inputs
    thread = app.state.threads["task-b"]
    app.telegram.send_message.reset_mock()
    assert await app._send_thread_text_reply(thread, text="B SECRET", reply_to_message_id=50) is None
    app.telegram.send_message.assert_not_called()


async def test_stale_or_forged_callback_cannot_select_another_agent(app):
    for user in [101, 999]:
        await app._handle_callback_query({"id": "callback", "data": "attach:known:1", "from": {"id": user}, "message": message("button")})
    assert not app.desktop.sent_inputs and not app.state.threads
    app.telegram.send_message.assert_not_called()


async def test_private_desktop_turn_not_mirrored_even_after_group_work(app):
    await app._attach_group(-1001, "task-a")
    thread = app.state.threads["task-a"]
    thread.relay_turn_ids = ["group-turn"]
    turn = make_turn("private-turn", "completed", items=[{"id": "private-reply", "type": "agentMessage", "text": "PRIVATE RESULT"}])
    app.desktop.threads["task-a"] = make_conversation("task-a", turns=[turn])
    await app._sync_thread("task-a")
    app.telegram.send_message.assert_not_called()


@pytest.mark.parametrize('chat,task', [(42, 'task-personal'), (-1001, 'task-a')])
@pytest.mark.parametrize('status', ['inProgress', 'completed', 'interrupted'])
async def test_desktop_turns_stay_off_telegram_in_every_chat(app, chat, task, status):
    app.state.bind_message(chat, 10, task)
    thread = app.state.threads[task]
    thread.relay_turn_ids = ['earlier-telegram-turn']
    turn = make_turn('desktop-turn', status, items=[
        {'id': 'desktop-input', 'type': 'userMessage', 'content': [{'type': 'text', 'text': 'Work requested in the app'}]},
        {'id': 'desktop-answer', 'type': 'agentMessage', 'text': 'App result: [File](/tmp/desktop-result.pdf)'},
    ])
    app.desktop.threads[task] = make_conversation(task, turns=[turn])
    app.telegram.send_file = AsyncMock()
    await app._sync_thread(task)
    app.state = BridgeState.load(app.config.bridge.state_path)
    await app._sync_thread(task)
    app.telegram.send_message.assert_not_called()
    app.telegram.send_file.assert_not_called()


@pytest.mark.parametrize('chat,task', [(42, 'task-personal'), (-1001, 'task-a')])
async def test_saved_desktop_reply_is_suppressed_after_restart(app, chat, task):
    app.state.bind_message(chat, 10, task)
    thread = app.state.threads[task]
    turn = make_turn('old-desktop-turn', 'completed', items=[
        {'id': 'answer', 'type': 'agentMessage', 'text': 'Previously queued desktop reply'},
    ])
    app._stage_terminal_output(thread, turn)
    app.state.save(app.config.bridge.state_path)
    app.state = BridgeState.load(app.config.bridge.state_path)
    thread = app.state.threads[task]
    await app._drain_outputs(thread)
    app.telegram.send_message.assert_not_called()
    saved = BridgeState.load(app.config.bridge.state_path)
    assert saved.threads[task].output_deliveries[turn.turn_id]['status'] == 'suppressed'


@pytest.mark.parametrize("incoming_text", ["/personal Confirm this", "Confirm this"])
async def test_private_telegram_request_still_gets_exactly_one_reply_after_restart(app, incoming_text):
    app.desktop.threads['task-personal'] = make_conversation('task-personal')
    incoming = message(incoming_text, chat=42, user=42)
    prompt = app._telegram_prompt(incoming, 'Confirm this', 'task-personal')
    turn = make_turn('telegram-turn', 'completed', items=[
        {'id': 'input', 'type': 'userMessage', 'content': [{'type': 'text', 'text': prompt}]},
        {'id': 'answer', 'type': 'agentMessage', 'text': 'Confirmed from Telegram.'},
    ])
    app.desktop.send_results[('task-personal', prompt)] = make_conversation('task-personal', turns=[turn])
    await app._handle_telegram_message(incoming)
    app.state = BridgeState.load(app.config.bridge.state_path)
    # Receipts continue to prove origin after the bounded relay-turn list rolls over.
    app.state.threads['task-personal'].relay_turn_ids = []
    await app._sync_thread('task-personal')
    app.state = BridgeState.load(app.config.bridge.state_path)
    await app._sync_thread('task-personal')
    app.telegram.send_message.assert_awaited_once()
    assert app.telegram.send_message.call_args.kwargs['text'] == 'Confirmed from Telegram.'
    assert app.telegram.send_message.call_args.kwargs['chat_id'] == 42
    assert app.telegram.send_message.call_args.kwargs['reply_to_message_id'] == 10


async def test_group_queue_is_not_marked_done_before_delivery(app):
    await app._attach_group(-1001, "task-a")
    thread = app.state.threads["task-a"]
    thread.pending_message_ids = [10, 11]
    thread.queued_inputs = [QueuedInput(-1001, 11, "Queued work")]
    app.state.bind_message(-1001, 10, 'task-a')
    thread.input_receipts['-1001:10'] = {'status': 'confirmed', 'turn_id': 'finished'}
    thread.output_deliveries['finished'] = {'status': 'sent'}
    await app._mark_pending_messages_done(thread)
    assert thread.pending_message_ids == [11]
    assert [c.kwargs["message_id"] for c in app.telegram.set_message_reaction.call_args_list] == [10]


@pytest.mark.parametrize('chat,user,task', [(-1001, 101, 'task-a'), (-1002, 202, 'task-b')])
async def test_reply_to_old_unbound_bot_notice_uses_only_assigned_group_agent(app, chat, user, task):
    app._send_thread_input = AsyncMock()
    incoming = message('Please confirm access', chat=chat, user=user, reply=77)
    incoming['reply_to_message']['from'] = {'id': 9000, 'is_bot': True}
    await app._handle_telegram_message(incoming)
    assert app._send_thread_input.call_args.args[0].thread_id == task
    assert app._send_thread_input.call_args.kwargs['text'].endswith('Message:\nPlease confirm access')


@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('reply_sender', [{}, {'id': 444, 'is_bot': False}, {'id': 9001, 'is_bot': True}, {'id': 9000, 'is_bot': False}])
async def test_non_bot_reply_target_does_not_route_even_with_saved_binding(app, reply_sender, bound):
    if bound:
        app.state.bind_message(-1001, 77, 'task-a')
        app.state.save(app.config.bridge.state_path)
        app.state = BridgeState.load(app.config.bridge.state_path)
    incoming = message('Human conversation', reply=77)
    incoming['reply_to_message']['from'] = reply_sender
    before = copy.deepcopy(app.state)
    await app._handle_telegram_message(incoming)
    assert not app.desktop.sent_inputs
    assert app.state == before
    app.telegram.send_message.assert_not_called()
    app.telegram.set_message_reaction.assert_not_called()


async def test_reply_to_human_does_not_follow_nested_bot_reply(app):
    incoming = message('A question for Jordan', reply=77,
                       reply_sender={'id': 444, 'is_bot': False})
    incoming['reply_to_message']['reply_to_message'] = {
        'message_id': 76, 'from': {'id': 9000, 'is_bot': True},
    }
    app.state.bind_message(-1001, 77, 'task-a')
    await app._handle_telegram_message(incoming)
    assert not app.desktop.sent_inputs
    app.telegram.set_message_reaction.assert_not_called()


async def test_reply_binding_cannot_replace_unknown_bot_identity(app):
    app._bot_id = None
    app.state.bind_message(-1001, 77, 'task-a')
    await app._handle_telegram_message(message('Continue', reply=77,
                                              reply_sender={'id': 9000, 'is_bot': True}))
    assert not app.desktop.sent_inputs


@pytest.mark.parametrize('enabled', [False, True])
async def test_reply_chain_option_survives_restart_and_applies_only_to_one_group(app, enabled):
    raw = copy.deepcopy(POLICY)
    raw['groups']['-1001']['follow_reply_chains'] = enabled
    path = app.config.bridge.state_path.parent / 'chat-routing.json'
    path.write_text(json.dumps(raw))
    app.routing = ChatRouting.load(42, path)
    app.state.bind_message(-1001, 77, 'task-a')
    app.state.bind_message(-1002, 77, 'task-b')
    app.state.save(app.config.bridge.state_path)
    app.state = BridgeState.load(app.config.bridge.state_path)
    app._send_thread_input = AsyncMock()
    for chat, user in [(-1001, 101), (-1002, 202)]:
        await app._handle_telegram_message(message('Continue', chat=chat, user=user,
            reply=77, reply_sender={'id': 444, 'is_bot': False}))
    assert app._send_thread_input.await_count == int(enabled)
    if enabled:
        assert app._send_thread_input.call_args.args[0].thread_id == 'task-a'
        assert app.state.lookup_thread_for_message(-1001, 10) == 'task-a'
        # Each accepted human reply can become the next link in the chain.
        await app._handle_telegram_message(message('One more thing', message_id=11,
            reply=10, reply_sender={'id': 101, 'is_bot': False}))
        assert app._send_thread_input.await_count == 2
        # Turning the option off stops new human replies without erasing history.
        app.routing.groups[-1001]['follow_reply_chains'] = False
        await app._handle_telegram_message(message('For another person', message_id=12,
            reply=11, reply_sender={'id': 101, 'is_bot': False}))
        assert app._send_thread_input.await_count == 2
        assert app.state.lookup_thread_for_message(-1001, 12) is None


@pytest.mark.parametrize('reply,bound,sender', [
    (None, True, {'id': 444, 'is_bot': False}),
    (77, False, {'id': 444, 'is_bot': False}),
    (77, True, {}),
    (77, True, {'id': 9001, 'is_bot': True}),
])
async def test_reply_chain_opt_in_still_requires_known_human_reply_target(app, reply, bound, sender):
    app.routing.groups[-1001]['follow_reply_chains'] = True
    if bound:
        app.state.bind_message(-1001, 77, 'task-a')
    await app._handle_telegram_message(message('Human conversation', reply=reply, reply_sender=sender))
    assert not app.desktop.sent_inputs
    app.telegram.set_message_reaction.assert_not_called()


@pytest.mark.parametrize('user,anonymous,task', [(999, False, 'task-a'), (101, True, 'task-a'), (101, False, 'task-b')])
async def test_reply_chain_opt_in_does_not_bypass_sender_or_agent_isolation(app, user, anonymous, task):
    app.routing.groups[-1001]['follow_reply_chains'] = True
    app.state.bind_message(-1001, 77, task)
    incoming = message('Continue', user=user, reply=77, reply_sender={'id': 444, 'is_bot': False})
    if anonymous:
        incoming['sender_chat'] = {'id': -1001}
    await app._handle_telegram_message(incoming)
    assert not app.desktop.sent_inputs


@pytest.mark.parametrize('text', ['/ask Continue', '/editor_a Continue', '@test_bot Continue'])
async def test_explicit_request_can_reply_to_human_message(app, text):
    app.state.bind_message(-1001, 77, 'task-a')
    app._send_thread_input = AsyncMock()
    incoming = message(text, reply=77, reply_sender={'id': 444, 'is_bot': False})
    if text.startswith('@'):
        incoming['entities'] = [{'type': 'mention', 'offset': 0, 'length': 9}]
    await app._handle_telegram_message(incoming)
    app._send_thread_input.assert_awaited_once()
    assert app._send_thread_input.call_args.args[0].thread_id == 'task-a'


@pytest.mark.parametrize('prefix', ['', '😀 ', '  '])
@pytest.mark.parametrize('caption', [False, True])
async def test_bot_mention_routes_human_group_member_to_only_assigned_agent(app, prefix, caption):
    app.routing.groups[-1001]['all_members'] = True
    app._send_thread_input = AsyncMock()
    incoming = message(prefix + '@TEST_BOT do you see my messages?', user=444)
    incoming['from'].update(first_name='Casey', username='editor_one')
    entities = [{'type':'mention', 'offset':len(prefix.encode('utf-16-le')) // 2, 'length':9}]
    if caption:
        incoming['caption'] = incoming.pop('text')
        incoming['caption_entities'] = entities
    else:
        incoming['entities'] = entities
    await app._handle_telegram_message(incoming)
    call = app._send_thread_input.call_args
    assert call.args[0].thread_id == 'task-a'
    assert 'Telegram sender: "Casey" (@editor_one)' in call.kwargs['text']
    assert call.kwargs['source_chat_id'] == -1001


@pytest.mark.parametrize('text,entities', [
    ('@other_bot hello', [{'type':'mention','offset':0,'length':10}]),
    ('@test_bot hello', []),
    ('@test_bot_extra hello', [{'type':'mention','offset':0,'length':15}]),
    ('@test_bot hello', [{'type':'mention','offset':-1,'length':9}]),
    ('@test_bot hello', [{'type':'mention','offset':0,'length':99}]),
    ('ordinary group conversation', []),
])
async def test_unaddressed_or_invalid_mentions_do_not_route(app, text, entities):
    incoming = message(text)
    incoming['entities'] = entities
    await app._handle_telegram_message(incoming)
    assert not app.desktop.sent_inputs


@pytest.mark.parametrize('chat,user', [(-9999,101), (-1001,999)])
async def test_mention_does_not_bypass_chat_or_sender_policy(app, chat, user):
    incoming = message('@test_bot hello', chat=chat, user=user)
    incoming['entities'] = [{'type':'mention','offset':0,'length':9}]
    await app._handle_telegram_message(incoming)
    assert not app.desktop.sent_inputs


async def test_all_members_can_reply_to_error_notice_and_identity_is_preserved(app):
    app.routing.groups[-1001]['all_members'] = True
    app.desktop.send_message = AsyncMock(side_effect=DesktopClientError('test failure'))
    await app._handle_telegram_message(message('/ask Start', user=333))
    # Transient failures are silent; only exhaustion produces one bound notice.
    for _ in range(7):
        app.state.threads['task-a'].queued_inputs[0].next_attempt_at = 0
        await app._attempt_queued_input(app.state.threads['task-a'])
    notice_id = app.state.threads['task-a'].last_chain_message_id
    assert notice_id != 10
    saved = BridgeState.load(app.config.bridge.state_path)
    assert saved.lookup_thread_for_message(-1001, notice_id) == 'task-a'
    app.state = saved
    app._send_thread_input = AsyncMock()
    incoming = message('Use the connected account.', user=444, message_id=11, reply=notice_id)
    incoming['reply_to_message']['from'] = {'id': 9000, 'is_bot': True}
    incoming['from'].update(first_name='Jordan', last_name='Example', username='editor_two')
    await app._handle_telegram_message(incoming)
    call = app._send_thread_input.call_args
    assert call.args[0].thread_id == 'task-a'
    prompt = call.kwargs['text']
    assert prompt.startswith('Telegram sender: "Jordan Example" (@editor_two)\n'
        'Telegram user ID: 444\nTelegram chat ID: -1001\nTelegram message ID: 11\n')
    assert 'Telegram agent: editor_a' in prompt and 'Telegram message ID: 11' in prompt
    assert prompt.endswith('Message:\nUse the connected account.')


async def test_identity_survives_queue_and_restart_for_distinct_group_members(app):
    app.routing.groups[-1001]['all_members'] = True
    await app._attach_group(-1001, 'task-a')
    app.state.threads['task-a'].current_turn_id = 'busy'
    for user, name, mid in [(101, 'Morgan', 10), (444, 'Jordan', 11)]:
        incoming = message('/ask Continue', user=user, message_id=mid)
        incoming['from']['first_name'] = name
        await app._handle_telegram_message(incoming)
    saved = BridgeState.load(app.config.bridge.state_path)
    queued = saved.threads['task-a'].queued_inputs
    assert len(queued) == 2
    assert '"Morgan"' in queued[0].text and 'Telegram user ID: 101\n' in queued[0].text
    assert '"Jordan"' in queued[1].text and 'Telegram user ID: 444\n' in queued[1].text
    assert all(job.text.endswith('Message:\nContinue') for job in queued)


async def test_reply_recovery_still_rejects_unauthorized_and_anonymous_members(app):
    for user, extra in [(444, {}), (101, {'sender_chat': {'id': -1001}})]:
        incoming = message('Use this account', user=user, reply=77)
        incoming['reply_to_message']['from'] = {'id': 9000, 'is_bot': True}
        incoming.update(extra)
        await app._handle_telegram_message(incoming)
    assert not app.desktop.sent_inputs


@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("default", ["omitted", None])
async def test_private_plain_text_requires_explicit_default(app, multiple, default):
    policy = copy.deepcopy(POLICY)
    policy.pop("default_private_agent")
    if default is None:
        policy["default_private_agent"] = None
    if multiple:
        policy["agents"]["another"] = {"thread_id": "task-another", "title": "Another"}
    app.routing = ChatRouting(42, policy)
    await app._handle_telegram_message(message("Hello", chat=42, user=42))
    assert not app.desktop.sent_inputs
    assert not app.state.threads
    assert "Personal" in app.telegram.send_message.call_args.kwargs["text"]
    assert "Just send a message here" not in app.telegram.send_message.call_args.kwargs["text"]
    if multiple:
        assert "Another" in app.telegram.send_message.call_args.kwargs["text"]


async def test_private_unknown_command_does_not_fall_back_to_default_agent(app):
    await app._handle_telegram_message(message("/missing Do work", chat=42, user=42))
    assert not app.desktop.sent_inputs
    assert not app.state.threads
    assert "not available" in app.telegram.send_message.call_args.kwargs["text"]


async def test_explicit_private_default_help_explains_plain_messages(app):
    await app._handle_telegram_message(message("/help", chat=42, user=42))
    text = app.telegram.send_message.call_args.kwargs["text"]
    assert "Just send a message here" in text
    assert "Editor A" not in text and "Editor B" not in text


async def test_plain_group_message_does_not_use_private_default(app):
    await app._handle_telegram_message(message("Hello"))
    assert not app.desktop.sent_inputs
    assert not app.state.threads
    app.telegram.send_message.assert_not_called()


@pytest.mark.parametrize("alias", ["missing", "editor_a", "", 123, [], True])
def test_invalid_private_default_fails_closed(alias):
    policy = copy.deepcopy(POLICY)
    policy["default_private_agent"] = alias
    with pytest.raises(ValueError, match="default_private_agent"):
        ChatRouting(42, policy)


@pytest.mark.parametrize("text,reply,expected", [
    ("Hello", None, "task-personal"),
    ("/another Hello", None, "task-another"),
    ("Hello", 77, "task-another"),
])
async def test_private_default_with_multiple_agents_respects_explicit_routes(app, text, reply, expected):
    policy = copy.deepcopy(POLICY)
    policy["agents"]["another"] = {"thread_id": "task-another", "title": "Another"}
    app.routing = ChatRouting(42, policy)
    if reply:
        app.state.bind_message(42, reply, "task-another")
    app._send_thread_input = AsyncMock()
    await app._handle_telegram_message(message(text, chat=42, user=42, reply=reply))
    assert app._send_thread_input.call_args.args[0].thread_id == expected


async def test_private_default_does_not_override_foreign_reply_binding(app):
    app.state.bind_message(42, 77, "task-a")
    await app._handle_telegram_message(message("Hello", chat=42, user=42, reply=77))
    assert not app.desktop.sent_inputs
    assert "not available" in app.telegram.send_message.call_args.kwargs["text"]


@pytest.mark.parametrize('value', ['true', 1, None])
def test_receive_all_messages_rejects_non_boolean_values(value):
    raw = copy.deepcopy(POLICY)
    raw['groups']['-1001']['receive_all_messages'] = value
    with pytest.raises(ValueError, match='receive_all_messages'):
        ChatRouting(42, raw)


@pytest.mark.parametrize('reply_sender', [None, {'id': 444, 'is_bot': False}, {'id': 9001, 'is_bot': True}])
async def test_all_messages_routes_plain_text_and_unbound_replies_only_to_opted_in_group(app, reply_sender):
    raw = copy.deepcopy(POLICY)
    raw['groups']['-1001']['receive_all_messages'] = True
    path = app.config.bridge.state_path.parent / 'chat-routing.json'
    path.write_text(json.dumps(raw))
    app.routing = ChatRouting.load(42, path)
    assert app.routing.groups[-1002]['receive_all_messages'] is False
    app._send_thread_input = AsyncMock()
    for chat, user in [(-1001, 101), (-1002, 202)]:
        await app._handle_telegram_message(message('Ordinary group message', chat=chat, user=user,
            reply=999 if reply_sender else None, reply_sender=reply_sender))
    app._send_thread_input.assert_awaited_once()
    call = app._send_thread_input.call_args
    assert call.args[0].thread_id == 'task-a'
    assert 'Telegram user ID: 101\n' in call.kwargs['text']
    assert call.kwargs['source_chat_id'] == -1001
    assert app.state.lookup_thread_for_message(-1002, 10) is None
    app.routing.groups[-1001]['receive_all_messages'] = False
    await app._handle_telegram_message(message('Not addressed', message_id=11))
    assert app._send_thread_input.await_count == 1


@pytest.mark.parametrize('case', ['unauthorized', 'anonymous', 'bot', 'unknown_group', 'other_command', 'wrong_agent'])
async def test_all_messages_preserves_sender_and_command_boundaries(app, case):
    app.routing.groups[-1001]['receive_all_messages'] = True
    incoming = message('Ordinary group message')
    if case == 'unauthorized': incoming['from']['id'] = 999
    if case == 'anonymous': incoming['sender_chat'] = {'id': -1001}
    if case == 'bot': incoming['from']['is_bot'] = True
    if case == 'unknown_group': incoming['chat']['id'] = -9999
    if case == 'other_command': incoming['text'] = '/ask@other_bot do work'
    if case == 'wrong_agent': incoming['text'] = '/editor_b do work'
    app._send_thread_input = AsyncMock()
    await app._handle_telegram_message(incoming)
    app._send_thread_input.assert_not_awaited()


async def test_all_messages_receives_standalone_attachment_and_deduplicates(app):
    app.routing.groups[-1001]['receive_all_messages'] = True
    async def download(**kwargs):
        kwargs['destination'].write_bytes(b'test')
    app.telegram.download_file = AsyncMock(side_effect=download)
    incoming = message('')
    incoming['document'] = {'file_id': 'test-file', 'file_name': 'test.txt', 'file_size': 4}
    await app._handle_telegram_message(incoming)
    assert len(app.desktop.sent_inputs) == 1
    assert 'test.txt' in app.desktop.sent_inputs[0][1]
    assert 'Telegram user ID: 101\n' in app.desktop.sent_inputs[0][1]
    await app._handle_telegram_message(incoming)
    assert len(app.desktop.sent_inputs) == 1
    app.telegram.download_file.assert_awaited_once()
