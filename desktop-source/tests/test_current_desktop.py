import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from codex_telegram_bridge.current_desktop import CurrentDesktopClient, matches_submitted_text, normalized_editor_text
from codex_telegram_bridge.desktop_client import DesktopClientError, DesktopDraftConflictError, DesktopSendUnconfirmedError, DesktopTurn, is_main_app_target
from codex_telegram_bridge.rollout_reader import RolloutError, RolloutReader

TASK = "00000000-0000-4000-8000-000000000004"


def event(kind, turn="turn-1", **kwargs):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn, **kwargs}}


def append(path, *rows):
    with path.open("ab") as handle:
        for row in rows:
            handle.write(json.dumps(row).encode() + b"\n")


@pytest.fixture
def log(tmp_path):
    path = tmp_path / "2026/09/28" / f"rollout-test-{TASK}.jsonl"
    path.parent.mkdir(parents=True)
    append(path, {"type": "session_meta", "payload": {"id": TASK, "cwd": "/example"}}, event("task_started"))
    return tmp_path, path


def test_renamed_main_window_excludes_auxiliary_pages():
    assert is_main_app_target({"type": "page", "title": "Video Buddy Editor A", "url": "app://-/index.html"})
    for url in ["app://-/index.html?initialRoute=%2Favatar-overlay", "app://-/detached-window.html", "https://example.com/index.html"]:
        assert not is_main_app_target({"type": "page", "title": "ChatGPT", "url": url})


@pytest.mark.parametrize("route", [
    "/space/local-page-example?window=page&prewarm=1",
    "/space/local-page-example?window=page",
    "/avatar-overlay?prewarm=1",
    "/detached-window?prewarm=1",
])
def test_auxiliary_initial_routes_do_not_block_main_chat(route):
    from urllib.parse import urlencode
    auxiliary = {"type": "page", "title": "ChatGPT",
                 "url": "app://-/index.html?" + urlencode({"initialRoute": route})}
    main = {"type": "page", "title": "Video Buddy Editor A", "url": "app://-/index.html"}
    assert [t for t in [auxiliary, main] if is_main_app_target(t)] == [main]


def test_main_window_can_open_a_space_without_becoming_auxiliary():
    assert is_main_app_target({"type": "page", "url": "app://-/index.html?initialRoute=%2Fspace%2Fexample"})


def test_only_public_user_and_final_messages_are_retained(log):
    root, path = log
    append(path,
        event("item_completed", thread_id=TASK, item={"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "hello"}]}),
        event("item_completed", thread_id="another", item={"type": "UserMessage", "content": [{"type": "text", "text": "wrong chat"}]}),
        event("item_completed", item={"type": "AgentMessage", "phase": "commentary", "content": [{"type": "Text", "text": "private progress"}]}),
        event("item_completed", item={"type": "CommandExecution", "text": "secret"}),
        event("item_completed", item={"type": "AgentMessage", "id": "a1", "phase": "final_answer", "content": [{"type": "Text", "text": "world"}]}),
        event("task_complete", last_agent_message="world"))
    reader = RolloutReader(TASK, root)
    turns = reader.refresh()
    assert turns[-1]["status"] == "completed"
    assert [i["id"] for i in turns[-1]["items"]] == ["u1", "a1"]
    assert "secret" not in json.dumps(turns) and "wrong chat" not in json.dumps(turns)


def test_partial_appends_and_large_tool_lines_do_not_duplicate_events(log):
    root, path = log
    reader = RolloutReader(TASK, root)
    with path.open("ab") as handle:
        handle.write(b'x' * (reader.MAX_LINE + 500) + b'\n')
        handle.write(json.dumps(event("task_complete", last_agent_message="done")).encode()[:-1])
    assert reader.refresh()[-1]["status"] == "inProgress"
    with path.open("ab") as handle:
        handle.write(b'}\n')
    assert reader.refresh()[-1]["status"] == "completed"
    offset = reader.offset
    assert reader.refresh()[-1]["items"][0]["text"] == "done"
    assert reader.offset == offset
    append(path, event("task_started", "turn-2"), event("turn_aborted", "turn-2"))
    assert reader.refresh()[-1]["status"] == "interrupted"


def test_large_historical_prefix_is_not_scanned(log, monkeypatch):
    root, path = log
    with path.open("ab") as handle:
        handle.write(b'x' * (1024 * 1024) + b'\n')
    append(path, event("task_complete", last_agent_message="recent"))
    monkeypatch.setattr(RolloutReader, "INITIAL_TAIL", 2048)
    reader = RolloutReader(TASK, root)
    assert reader.refresh()[-1]["items"][0]["text"] == "recent"
    assert reader.offset == path.stat().st_size
    path.write_bytes(b"truncated")
    with pytest.raises(RolloutError, match="truncated"):
        reader.refresh()


def test_identity_mismatch_fails_closed(log):
    root, path = log
    content = path.read_text().replace(TASK, "unrelated-task")
    path.write_text(content)
    with pytest.raises(RolloutError, match="identity"):
        RolloutReader(TASK, root)


async def test_busy_task_never_changes_desktop(log):
    root, path = log
    client = CurrentDesktopClient(task_titles={TASK: "Video Buddy Editor A"}, sessions_path=root,
        app_path=Path("/Applications/ChatGPT.app"), remote_debugging_port=9229,
        user_data_dir=Path("/tmp/unused"), launch_timeout_seconds=1, send_ack_timeout_seconds=1, poll_interval_seconds=.01)
    client._eval_json = AsyncMock()
    try:
        with pytest.raises(DesktopClientError, match="still running"):
            await client.send_message(TASK, "test")
        client._eval_json.assert_not_called()
        with pytest.raises(DesktopClientError, match="not registered"):
            await client.read_thread("unrelated")
    finally:
        await client.close()


async def test_existing_draft_blocks_switch(log):
    root, _ = log
    client = CurrentDesktopClient(task_titles={TASK: "Editor A"}, sessions_path=root,
        app_path=Path("/Applications/ChatGPT.app"), remote_debugging_port=9229,
        user_data_dir=Path("/tmp/unused"), launch_timeout_seconds=1, send_ack_timeout_seconds=1, poll_interval_seconds=.01)
    client._current_thread_id = AsyncMock(return_value="another-task")
    client.read_composer_state = AsyncMock(return_value={"ok": True, "text": "unsent work"})
    client._eval_json = AsyncMock()
    try:
        with pytest.raises(DesktopDraftConflictError):
            await client.activate_thread(TASK)
        client._eval_json.assert_not_called()
    finally:
        await client.close()


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is needed for the editor script')
@pytest.mark.parametrize('text,missing', [('', False), ('Actual unsent text', False), ('First\nSecond', False), ('\ufffc', False), ('', True)])
async def test_editor_document_excludes_suggestions_and_preserves_real_drafts(idle_client, text, missing):
    client, _ = idle_client
    client._eval_json = AsyncMock(return_value={'ok':False})
    await client.read_composer_state()
    expression=client._eval_json.call_args.args[0]
    fixture=r'''
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const composer={innerText:input.text+'Suggested reply\nPress Tab to accept',
  getBoundingClientRect:()=>({width:200,height:50}),closest:()=>({})};
if(!input.missing) composer.pmViewDesc={node:{type:{name:'doc'},content:{size:input.text.length},textBetween:()=>input.text}};
global.getComputedStyle=()=>({visibility:'visible',display:'block'});
global.document={querySelectorAll:()=>[composer]};
process.stdout.write(JSON.stringify(eval(input.expression)));
'''
    run=subprocess.run([shutil.which('node'),'-e',fixture],text=True,capture_output=True,
        input=json.dumps(dict(text=text,missing=missing,expression=expression)),check=True)
    result=json.loads(run.stdout)
    if missing:
        assert result=={'ok':False,'error':'missing-editor-document'}
    else:
        assert result=={'ok':True,'text':text}


@pytest.fixture
async def idle_client(log):
    root, path = log
    append(path, event("task_complete", last_agent_message="earlier reply"))
    client = CurrentDesktopClient(task_titles={TASK: "Editor A"}, sessions_path=root,
        app_path=Path("/Applications/ChatGPT.app"), remote_debugging_port=9229,
        user_data_dir=Path("/tmp/unused"), launch_timeout_seconds=.5, send_ack_timeout_seconds=.01, poll_interval_seconds=.01)
    yield client, path
    await client.close()


async def test_waits_for_delayed_editor_and_send_button(idle_client):
    client, path = idle_client
    replies = [{"ok": False, "error": "composer-updating"}, {"ok": False, "error": "send-not-ready"}, {"ok": True}]
    async def evaluate(expression):
        result = replies.pop(0)
        if result["ok"]:
            append(path, event("task_started", "turn-2"), event("item_completed", "turn-2", item={"type": "UserMessage", "id": "u2", "content": [{"type": "text", "text": "follow-up"}]}))
        return result
    client._eval_json = AsyncMock(side_effect=evaluate)
    result = await client._submit_and_wait(TASK, "follow-up", {"turn-1"})
    assert result.latest_turn.turn_id == "turn-2"
    assert client._eval_json.call_count == 3
    assert not replies


async def test_unknown_ack_never_repeats_click(idle_client):
    client, _ = idle_client
    client._eval_json = AsyncMock(return_value={"ok": True})
    with pytest.raises(DesktopSendUnconfirmedError):
        await client._submit_and_wait(TASK, "follow-up", {"turn-1"})
    client._eval_json.assert_awaited_once()


async def test_changed_task_cancels_without_retry(idle_client):
    client, _ = idle_client
    client._eval_json = AsyncMock(return_value={"ok": False, "error": "wrong-task"})
    with pytest.raises(DesktopClientError, match="wrong-task"):
        await client._submit_and_wait(TASK, "follow-up", {"turn-1"})
    client._eval_json.assert_awaited_once()


async def test_new_turn_during_button_wait_stops_submission(idle_client):
    client, path = idle_client
    async def evaluate(expression):
        append(path, event("task_started", "someone-elses-turn"))
        return {"ok": False, "error": "send-not-ready"}
    client._eval_json = AsyncMock(side_effect=evaluate)
    with pytest.raises(DesktopClientError, match="still running"):
        await client._submit_and_wait(TASK, "follow-up", {"turn-1"})
    client._eval_json.assert_awaited_once()


async def test_recovery_never_inserts_or_submits_a_different_draft(idle_client):
    client, _ = idle_client
    client.activate_thread = AsyncMock()
    client.read_composer_state = AsyncMock(return_value={"ok": True, "text": "someone else's draft"})
    client._call_cdp = AsyncMock()
    with pytest.raises(DesktopClientError, match="Recovery draft differs"):
        await client.send_existing_draft(TASK, "known failed reply")
    client._call_cdp.assert_not_called()


async def test_attachment_draft_recovery_accepts_observed_editor_spacing(idle_client):
    client, _ = idle_client
    expected = 'confirm image receipt\n\nTelegram attachments saved locally:\n- "/project with spaces/01-photo.jpg"\nRead this file.'
    rendered = 'confirm image receipt\n\n\n\n\nTelegram attachments saved locally:\n\n- "/project with spaces/01-photo.jpg"\n\nRead this file.'
    client.activate_thread = AsyncMock()
    client.read_composer_state = AsyncMock(return_value={"ok": True, "text": rendered})
    client._submit_and_wait = AsyncMock(return_value="confirmed")
    assert await client.send_existing_draft(TASK, expected) == "confirmed"
    client._submit_and_wait.assert_awaited_once_with(TASK, expected, {"turn-1"})
    assert normalized_editor_text(rendered) == normalized_editor_text(expected)
    for changed in (expected.replace('01-photo', '02-photo'), expected.replace('with spaces', 'with  spaces'), expected.replace('\nRead', ' Read')):
        client.read_composer_state.return_value = {"ok": True, "text": changed}
        with pytest.raises(DesktopClientError, match="Recovery draft differs"):
            await client.send_existing_draft(TASK, expected)
    assert client._submit_and_wait.await_count == 1


def test_acknowledgment_accepts_only_editor_blank_line_normalization():
    turn = DesktopTurn(turn_id="new", status="inProgress", items=[{"type": "userMessage", "content": [{"type": "text", "text": "Your project space is called Project D\nDo you see it?\n"}]}])
    assert matches_submitted_text(turn, "Your project space is called Project D\n\nDo you see it?")
    assert not matches_submitted_text(turn, "Your project space is called Project E\n\nDo you see it?")
    assert not matches_submitted_text(turn, "Your project space is called Project D Do you see it?")
    assert not matches_submitted_text(turn, "Your  project space is called Project D\nDo you see it?")


def test_acknowledgment_handles_editor_auto_link_formatting():
    url = 'https://example.com/folder/a_b'
    turn = DesktopTurn(turn_id='new', status='inProgress', items=[{'type':'userMessage','content':[{'type':'text','text':r'[https://example.com/folder/a\_b](https://example.com/folder/a_b)'}]}])
    assert matches_submitted_text(turn, url)
    assert not matches_submitted_text(turn, 'https://example.com/folder/different')
    assert not matches_submitted_text(turn, '[click here](https://example.com/folder/a_b)')


@pytest.mark.parametrize('url,serialized', [
    ('https://example.com/a_b?key=1&dl=0',
     r'[https://example.com/a_b?key=1&dl=0](https://example.com/a_b?key=1\&dl=0)'),
    ('https://example.com/a_(b)?key=1&dl=0',
     r'[https://example.com/a\_\(b\)?key=1&dl=0](https://example.com/a_\(b\)?key=1\&dl=0)'),
    ('https://example.com/a?literal=\\_&dl=0',
     r'[https://example.com/a?literal=\\\_&dl=0](https://example.com/a?literal=\\\_\&dl=0)'),
])
def test_acknowledgment_decodes_autolink_targets_once(url, serialized):
    prefix = 'Telegram user ID: 101\nTelegram chat ID: -1001\nTelegram message ID: 12\nMessage:\n'
    saved = prefix + serialized + '\n1\\. Review footage'
    turn = DesktopTurn(turn_id='new', status='inProgress', items=[{
        'type':'userMessage', 'content':[{'type':'text','text':saved}]}])
    expected = prefix + url + '\n1. Review footage'
    assert matches_submitted_text(turn, expected)
    for old, new in [('dl=0','dl=1'), ('ID: 12','ID: 19'), ('ID: -1001','ID: -1002'), ('ID: 101','ID: 102')]:
        assert not matches_submitted_text(turn, expected.replace(old, new))
    if '\\_' in expected:
        assert not matches_submitted_text(turn, expected.replace('\\_', '_'))


def test_acknowledgment_does_not_unwrap_different_link_destination():
    saved = r'[https://example.com/?a=1&b=2](https://other.example/?a=1\&b=2)'
    turn = DesktopTurn(turn_id='new', status='inProgress', items=[{
        'type':'userMessage', 'content':[{'type':'text','text':saved}]}])
    assert not matches_submitted_text(turn, 'https://example.com/?a=1&b=2')


def test_acknowledgment_handles_serialized_attachment_bullet():
    expected = 'test\n\nTelegram attachments saved locally:\n- "/project with spaces/01-photo.jpg"\nRead this file.'
    saved = expected.replace('\n- ', '\n\\- ')
    turn = DesktopTurn(turn_id='new', status='inProgress', items=[{'type':'userMessage', 'content':[{'type':'text', 'text':saved}]}])
    assert matches_submitted_text(turn, expected)
    assert not matches_submitted_text(turn, expected.replace('01-photo', '02-photo'))
    assert not matches_submitted_text(turn, expected.replace('\n- ', '\n+ '))


def test_ambient_browser_envelope_does_not_hide_exact_telegram_receipt():
    expected = 'Telegram sender: "Casey" (@editor_one)\nTelegram user ID: 444\nTelegram chat ID: -1001\nTelegram message ID: 35\nTelegram bridge: abc\nTelegram agent: editor\n\nMessage:\nI choose 3. Deep techno ambience'
    prefix = '\n<in-app-browser-context source="ambient-ui-state">\nCurrent URL: https://example.com\n</in-app-browser-context>\n\n## My request:\n'
    def turn(text):
        return DesktopTurn(turn_id='new',status='inProgress',items=[{
            'type':'userMessage','content':[{'type':'text','text':text}]}])
    assert matches_submitted_text(turn(prefix + expected + '\n'), expected)
    assert not matches_submitted_text(turn(prefix + expected), expected.replace('ID: 35','ID: 39'))
    assert not matches_submitted_text(turn(prefix + expected), expected.replace('3. Deep','2. Deep'))
    assert not matches_submitted_text(turn(prefix + expected + '\nAdditional user instruction'), expected)
    assert not matches_submitted_text(turn('Quoted message:\n' + prefix + expected), expected)
    assert not matches_submitted_text(turn(prefix.replace('ambient-ui-state','unrecognized') + expected), expected)
    assert not matches_submitted_text(turn(prefix.replace('https://example.com',expected) + 'different request'), expected)


async def test_pending_input_recovery_expands_history_after_restart(log, monkeypatch):
    root, path = log
    expected = 'Telegram sender: "Casey"\nTelegram message ID: 19\nMessage:\nCan you see this?'
    append(path, event('item_completed',item={'type':'UserMessage','id':'old-input','content':[{'type':'text','text':expected}]}),
        event('task_complete',last_agent_message='Earlier reply'))
    with path.open('ab') as handle:
        handle.write(b'x' * 3000 + b'\n')
    append(path,event('task_started','turn-2'),event('task_complete','turn-2',last_agent_message='Recent reply'))
    monkeypatch.setattr(RolloutReader,'INITIAL_TAIL',1024)
    monkeypatch.setattr(RolloutReader,'MAX_TAIL',8192)
    client = CurrentDesktopClient(task_titles={TASK:'Editor A'},sessions_path=root,
        app_path=Path('/Applications/ChatGPT.app'),remote_debugging_port=9229,
        user_data_dir=Path('/tmp/unused'),launch_timeout_seconds=1,send_ack_timeout_seconds=1,poll_interval_seconds=.01)
    client._call_cdp = AsyncMock()
    try:
        initial = await client.read_thread(TASK)
        assert not any(matches_submitted_text(t,expected) for t in initial.turns)
        recovered = await client.recover_submitted_inputs(TASK,[expected])
        assert [t.turn_id for t in recovered.turns if matches_submitted_text(t,expected)] == ['turn-1']
        assert client._readers[TASK].history_start == 0
        client._call_cdp.assert_not_called()
    finally:
        await client.close()


def test_history_expansion_respects_cap_and_does_not_repeat_scan(log, monkeypatch):
    root,path=log
    with path.open('ab') as handle:handle.write(b'x'*12000+b'\n')
    append(path,event('task_complete',last_agent_message='Recent'))
    monkeypatch.setattr(RolloutReader,'INITIAL_TAIL',1024)
    monkeypatch.setattr(RolloutReader,'MAX_TAIL',4096)
    reader=RolloutReader(TASK,root)
    assert reader.expand_history()
    assert reader.expand_history()
    assert not reader.expand_history()
    assert reader.history_window == 4096 and reader.history_start > 0


def test_acknowledgment_handles_sender_names_without_erasing_literal_backslashes():
    expected = 'Telegram sender: "Jordan [editor]" (@editor_two)\nTelegram user ID: 444\n\nMessage:\nKeep literal \\_underscores\\_'
    saved = r'Telegram sender: "Jordan \[editor\]" (@editor\_two)' + '\nTelegram user ID: 444\nMessage:\n' + r'Keep literal \\\_underscores\\\_'
    turn = DesktopTurn(turn_id='new', status='inProgress', items=[{'type':'userMessage', 'content':[{'type':'text', 'text':saved}]}])
    assert matches_submitted_text(turn, expected)
    assert not matches_submitted_text(turn, expected.replace('444', '445'))
    assert not matches_submitted_text(turn, expected.replace('\\_', '_'))


async def test_multiline_reply_is_acknowledged_after_serialization(idle_client):
    client, path = idle_client
    async def evaluate(expression):
        append(path, event("task_started", "turn-2"), event("item_completed", "turn-2", item={"type": "UserMessage", "id": "u2", "content": [{"type": "text", "text": "Paragraph one\nParagraph two\n"}]}))
        return {"ok": True}
    client._eval_json = AsyncMock(side_effect=evaluate)
    result = await client._submit_and_wait(TASK, "Paragraph one\n\nParagraph two", {"turn-1"})
    assert result.latest_turn.turn_id == "turn-2"
    client._eval_json.assert_awaited_once()
