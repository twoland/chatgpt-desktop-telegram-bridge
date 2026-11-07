import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import codex_telegram_bridge.telegram_api as api_module
from codex_telegram_bridge.media import (
    ArtifactBlocked, IncomingFile, LocalArtifact, artifact_key, incoming_files,
    local_artifacts, receive_files, safe_filename, upload_kind, validate_artifact,
)
from codex_telegram_bridge.state import BridgeState
from codex_telegram_bridge.telegram_api import SentMessage, TelegramApiError, TelegramBotApi, TelegramFileTooLarge
from test_bridge import make_conversation, make_turn
from test_chat_isolation import app, message


def test_selects_largest_photo_and_supports_file_types():
    selected = incoming_files({"photo": [{"file_id": "small", "width": 10, "height": 10}, {"file_id": "large", "width": 500, "height": 500}]})
    assert selected[0].file_id == "large"
    for kind in ["document", "video", "animation", "audio", "voice", "video_note", "sticker"]:
        assert incoming_files({kind: {"file_id": kind}})[0].kind == kind


def test_file_links_with_spaces_parens_unicode_and_code_examples():
    text = '''![Preview](</Projects/Project D/cut (2).mp4>)
[Original](/Projects/Project%20D/photo.jpg)
[Again](/Projects/Project%20D/photo.jpg)
[Note](/Projects/Пример/report.pdf)
[Code](/Projects/main.py:20)
`[Ignore](/Projects/example.txt)`
```md
[Ignore](/Projects/secret.txt)
```
![Remote](https://example.com/remote.png)'''
    files = local_artifacts(text)
    assert [str(f.path) for f in files] == ["/Projects/Project D/cut (2).mp4", "/Projects/Project D/photo.jpg", "/Projects/Пример/report.pdf"]
    assert files[0].embedded and not files[1].embedded


def test_exports_stay_in_agent_roots_and_reject_symlink_escape(tmp_path):
    root = tmp_path / "project"; root.mkdir()
    outside = tmp_path / "outside.txt"; outside.write_text("private")
    (root / "link.txt").symlink_to(outside)
    for path in [outside, root / "link.txt"]:
        with pytest.raises(ArtifactBlocked):
            validate_artifact(LocalArtifact(path, False), [root])
    (root / ".env").write_text("credential")
    with pytest.raises(ArtifactBlocked):
        validate_artifact(LocalArtifact(root / ".env", False), [root])
    good = root / "render.mp4"; good.write_bytes(b"video")
    assert validate_artifact(LocalArtifact(good, False), [root]) == good
    assert upload_kind(LocalArtifact(good, False), good) == "video"
    photo = root / "photo.jpg"; photo.write_bytes(b"image")
    assert upload_kind(LocalArtifact(photo, False), photo) == "document"
    assert upload_kind(LocalArtifact(photo, True), photo) == "photo"


async def test_incoming_paths_are_sanitized_scoped_and_retry_cached(tmp_path):
    api = AsyncMock()
    async def download(**kwargs):
        kwargs["destination"].write_bytes(b"contents")
    api.download_file.side_effect = download
    files = [IncomingFile("file-1", "../../sample.txt", "document", 8)]
    a = await receive_files(api, base=tmp_path, thread_id="task-a", chat_id=-1001, message_id=10, attachments=files)
    b = await receive_files(api, base=tmp_path, thread_id="task-b", chat_id=-1002, message_id=10, attachments=files)
    assert a != b and a[0].name == "01-sample.txt"
    assert all(p.is_relative_to(tmp_path) for p in a + b)
    assert await receive_files(api, base=tmp_path, thread_id="task-a", chat_id=-1001, message_id=10, attachments=files) == a
    assert api.download_file.call_count == 2
    assert safe_filename('..\\..\\my\nfile.txt') == "my_file.txt"


async def test_api_download_and_upload_original_bytes(tmp_path):
    calls = []
    async def handler(request):
        calls.append(request)
        if request.url.path.endswith('/getFile'):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/source.txt", "file_size": 7}})
        if '/file/' in request.url.path:
            return httpx.Response(200, content=b"payload")
        assert b"payload" in await request.aread()
        assert b'name="document"' in request.content
        return httpx.Response(200, json={"ok": True, "result": {"chat": {"id": -1001}, "message_id": 51}})
    api = TelegramBotApi("123:fake")
    await api._client.aclose(); api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        target = tmp_path / "test.txt"
        await api.download_file(file_id="remote-id", destination=target, expected_size=7)
        assert target.read_bytes() == b"payload" and target.stat().st_mode & 0o777 == 0o600
        sent = await api.send_file(chat_id=-1001, path=target, reply_to_message_id=50)
        assert sent.message_id == 51 and len(calls) == 3
    finally:
        await api.close()


async def test_streaming_size_limit_cleans_partial_and_redacts_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "MAX_DOWNLOAD_BYTES", 4)
    async def handler(request):
        if request.url.path.endswith('/getFile'):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "files/a.txt"}})
        return httpx.Response(200, content=b"too large")
    api = TelegramBotApi("123:PRIVATE_TOKEN")
    await api._client.aclose(); api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        target = tmp_path / "a.txt"
        with pytest.raises(TelegramFileTooLarge):
            await api.download_file(file_id="id", destination=target)
        assert not target.exists() and not target.with_name("a.txt.partial").exists()
        async def failure(request):
            raise httpx.ConnectError("secret URL " + str(request.url), request=request)
        await api._client.aclose(); api._client = httpx.AsyncClient(transport=httpx.MockTransport(failure))
        with pytest.raises(TelegramApiError) as caught:
            await api.get_me()
        assert "PRIVATE_TOKEN" not in str(caught.value)
    finally:
        await api.close()


async def test_authorization_before_attachment_download(app):
    app.telegram.download_file = AsyncMock()
    item = message("", chat=-1001, user=999)
    item['document'] = {"file_id": "secret", "file_name": "clip.mp4"}
    await app._handle_telegram_message(item)
    app.telegram.download_file.assert_not_called()
    assert not app.desktop.sent_inputs


@pytest.mark.parametrize('caption', ['', 'A photo for Jordan', '@other_bot inspect this'])
@pytest.mark.parametrize('reply', [None, 10])
async def test_unaddressed_group_attachment_is_not_downloaded_or_routed(app, caption, reply):
    app.telegram.download_file = AsyncMock()
    app._send_thread_input = AsyncMock()
    app.state.bind_message(-1001, 10, 'task-a')
    item = message('', message_id=12, reply=reply, reply_sender={'id': 444, 'is_bot': False})
    item['caption'] = caption
    if caption.startswith('@'):
        item['caption_entities'] = [{'type': 'mention', 'offset': 0, 'length': 10}]
    item['document'] = {'file_id': 'doc', 'file_name': 'brief.pdf', 'file_size': 4}
    await app._handle_telegram_message(item)
    app.telegram.download_file.assert_not_called()
    app._send_thread_input.assert_not_called()
    app.telegram.send_message.assert_not_called()
    app.telegram.set_message_reaction.assert_not_called()
    assert app.state.lookup_thread_for_message(-1001, 12) is None


@pytest.mark.parametrize('address', ['reply', 'mention', 'command', 'private'])
async def test_explicitly_addressed_attachment_is_downloaded_and_routed(app, address):
    async def download(**kwargs):
        kwargs["destination"].write_bytes(b"file")
    app.telegram.download_file = AsyncMock(side_effect=download)
    app._send_thread_input = AsyncMock()
    item = message('')
    if address == 'reply':
        item['reply_to_message'] = {'message_id': 77, 'from': {'id': 9000, 'is_bot': True}}
    elif address == 'mention':
        item['caption'] = '@test_bot Inspect this'
        item['caption_entities'] = [{'type': 'mention', 'offset': 0, 'length': 9}]
    elif address == 'command':
        item['caption'] = '/ask@test_bot Inspect this'
    else:
        item = message('', chat=42, user=42)
    item['document'] = {"file_id": "doc", "file_name": "brief.pdf", "file_size": 4}
    await app._handle_telegram_message(item)
    call = app._send_thread_input.call_args
    assert call.args[0].thread_id == ('task-personal' if address == 'private' else 'task-a')
    app.telegram.download_file.assert_awaited_once()
    assert '01-brief.pdf' in call.kwargs['text'] and 'Telegram attachments' in call.kwargs['text']


@pytest.mark.parametrize('reply', [None, 77])
async def test_reply_chain_opt_in_allows_bound_human_attachment_reply_only(app, reply):
    app.routing.groups[-1001]['follow_reply_chains'] = True
    app.state.bind_message(-1001, 77, 'task-a')
    async def download(**kwargs):
        kwargs['destination'].write_bytes(b'file')
    app.telegram.download_file = AsyncMock(side_effect=download)
    app._send_thread_input = AsyncMock()
    incoming = message('', reply=reply, reply_sender={'id': 444, 'is_bot': False})
    incoming['document'] = {'file_id': 'doc', 'file_name': 'brief.pdf', 'file_size': 4}
    await app._handle_telegram_message(incoming)
    assert app.telegram.download_file.await_count == int(reply is not None)
    assert app._send_thread_input.await_count == int(reply is not None)


@pytest.mark.parametrize('escaped_bullet', [False, True])
async def test_private_attachment_prompt_not_echoed_without_ack_or_after_restart(app, escaped_bullet):
    async def download(**kwargs):
        kwargs['destination'].write_bytes(b'image')
    app.telegram.download_file = AsyncMock(side_effect=download)
    app._send_thread_input = AsyncMock()  # No acknowledged turn or handled key.
    incoming = message('', chat=42, user=42)
    incoming['caption'] = '/personal Inspect this'
    incoming['photo'] = [{'file_id': 'photo', 'width': 100, 'height': 100}]
    await app._handle_telegram_message(incoming)
    prompt = app._send_thread_input.call_args.kwargs['text']
    if escaped_bullet:
        prompt = prompt.replace('\n- ', '\n\\- ')
    turn = make_turn('received', 'inProgress', items=[{
        'id': 'input', 'type': 'userMessage', 'content': [{'type': 'text', 'text': prompt}],
    }])
    app.desktop.threads['task-personal'] = make_conversation('task-personal', turns=[turn])
    app.state.save(app.config.bridge.state_path)
    app.state = BridgeState.load(app.config.bridge.state_path)
    await app._sync_thread('task-personal')
    app.telegram.send_message.assert_not_called()
    saved = BridgeState.load(app.config.bridge.state_path)
    assert saved.threads['task-personal'].last_handled_user_input_key
    app.state = saved
    await app._sync_thread('task-personal')
    app.telegram.send_message.assert_not_called()
    done = make_turn('received', 'completed', items=turn.items + [{
        'id': 'answer', 'type': 'agentMessage', 'text': 'I received your image.',
    }])
    app.desktop.threads['task-personal'] = make_conversation('task-personal', turns=[done])
    await app._sync_thread('task-personal')
    app.telegram.send_message.assert_not_called()
    # A prompt's shape alone is not proof of Telegram origin. The delivery
    # queue must reconcile it and record a confirmed receipt first.
    app.state.threads['task-personal'].input_receipts['42:10'] = {
        'status': 'confirmed', 'turn_id': 'received',
    }
    await app._sync_thread('task-personal')
    app.telegram.send_message.assert_awaited_once()
    assert app.telegram.send_message.call_args.kwargs['text'] == 'I received your image.'


async def test_ordinary_desktop_input_is_not_mirrored_to_private_chat(app):
    app.state.bind_message(42, 10, 'task-personal')
    turn = make_turn('desktop-input', 'inProgress', items=[{
        'id': 'input', 'type': 'userMessage', 'content': [{'type': 'text', 'text': 'Continue the edit'}],
    }])
    app.desktop.threads['task-personal'] = make_conversation('task-personal', turns=[turn])
    await app._sync_thread('task-personal')
    app.telegram.send_message.assert_not_called()


async def test_private_sender_identity_is_not_echoed_after_unconfirmed_text_send(app):
    app._send_thread_input = AsyncMock()
    incoming = message('/personal Continue the edit', chat=42, user=42)
    incoming['from'].update(first_name='Morgan', last_name='Woland', username='theodor_w')
    await app._handle_telegram_message(incoming)
    prompt = app._send_thread_input.call_args.kwargs['text']
    turn = make_turn('received', 'inProgress', items=[{
        'id': 'input', 'type': 'userMessage', 'content': [{'type': 'text', 'text': prompt}],
    }])
    app.desktop.threads['task-personal'] = make_conversation('task-personal', turns=[turn])
    app.state = BridgeState.load(app.config.bridge.state_path)
    await app._sync_thread('task-personal')
    app.telegram.send_message.assert_not_called()
    assert app.state.threads['task-personal'].last_handled_user_input_key


async def test_oversize_incoming_gets_notice_without_dispatch(app):
    app.telegram.download_file = AsyncMock(side_effect=TelegramFileTooLarge("20 MB download limit"))
    item = message('/ask Read this'); item['document'] = {"file_id": "big", "file_name": "big.mov"}
    await app._handle_telegram_message(item)
    assert not app.desktop.sent_inputs
    assert '20 MB' in app.telegram.send_message.call_args.kwargs['text']


async def prepare_artifact_turn(app, tmp_path, *, turn_id='group-turn'):
    path = tmp_path / "result.txt"; path.write_text("result")
    turn = make_turn(turn_id, 'completed', items=[{'id': 'result', 'type': 'agentMessage', 'text': f'Done: [File](<{path}>)'}])
    conversation = make_conversation('task-a', turns=[turn]); conversation.cwd = str(tmp_path)
    app.desktop.threads['task-a'] = conversation
    app.routing.file_roots['task-a'] = [tmp_path]
    await app._attach_group(-1001, 'task-a')
    thread = app.state.threads['task-a']; thread.relay_turn_ids = ['group-turn']
    return thread, turn, path


async def test_file_delivery_is_bound_to_group_and_persisted_without_duplicates(app, tmp_path):
    thread, turn, path = await prepare_artifact_turn(app, tmp_path)
    app.telegram.send_file = AsyncMock(return_value=SentMessage(-1001, 900, {}))
    assert await app._deliver_terminal_turn(thread, turn)
    app.telegram.send_file.assert_awaited_once()
    assert app.telegram.send_file.call_args.kwargs['chat_id'] == -1001
    assert app.state.lookup_thread_for_message(-1001, 900) == 'task-a'
    app.state = BridgeState.load(app.config.bridge.state_path)
    assert not app._terminal_turn_needs_delivery(app.state.threads['task-a'], turn)


async def test_unrelated_desktop_turn_never_exports_file(app, tmp_path):
    thread, turn, _ = await prepare_artifact_turn(app, tmp_path, turn_id='private-turn')
    app.telegram.send_file = AsyncMock()
    assert not await app._deliver_terminal_turn(thread, turn)
    app.telegram.send_file.assert_not_called()
    app.telegram.send_message.assert_not_called()


async def test_uncertain_file_upload_is_not_repeated_after_restart(app, tmp_path):
    thread, turn, _ = await prepare_artifact_turn(app, tmp_path)
    app.telegram.send_file = AsyncMock(side_effect=TelegramApiError('Upload could not be confirmed'))
    assert await app._deliver_terminal_turn(thread, turn)
    app.state = BridgeState.load(app.config.bridge.state_path)
    await app._deliver_terminal_turn(app.state.threads['task-a'], turn)
    app.telegram.send_file.assert_awaited_once()
    assert any('Could not attach' in c.kwargs['text'] for c in app.telegram.send_message.call_args_list)


async def test_incomplete_upload_receipt_prevents_replay(app, tmp_path):
    thread, turn, path = await prepare_artifact_turn(app, tmp_path)
    artifact = LocalArtifact(path, False)
    thread.media_deliveries[artifact_key(turn.turn_id, 'result', artifact)] = {'status': 'uploading', 'name': path.name, 'notice_sent': False}
    app.telegram.send_file = AsyncMock()
    assert await app._deliver_terminal_turn(thread, turn)
    app.telegram.send_file.assert_not_called()


async def test_outgoing_size_limit_reports_without_uploading(app, tmp_path):
    thread, turn, path = await prepare_artifact_turn(app, tmp_path)
    with path.open('wb') as handle:
        handle.truncate(api_module.MAX_UPLOAD_BYTES + 1)
    app.telegram.send_file = AsyncMock()
    assert await app._deliver_terminal_turn(thread, turn)
    app.telegram.send_file.assert_not_called()
    assert '50 MB' in app.telegram.send_message.call_args.kwargs['text']


async def test_another_agents_project_file_is_not_uploaded(app, tmp_path):
    thread, turn, path = await prepare_artifact_turn(app, tmp_path)
    private_root = tmp_path / 'other-agent'; private_root.mkdir()
    app.desktop.threads['task-a'].cwd = str(private_root)
    app.routing.file_roots['task-a'] = [private_root]
    app.routing.file_roots['task-b'] = [tmp_path]
    app.telegram.send_file = AsyncMock()
    assert await app._deliver_terminal_turn(thread, turn)
    app.telegram.send_file.assert_not_called()
    assert 'outside' in app.telegram.send_message.call_args.kwargs['text']
