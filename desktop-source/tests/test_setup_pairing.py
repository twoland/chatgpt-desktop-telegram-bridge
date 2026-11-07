import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from codex_telegram_bridge.chat_routing import ChatRouting

TASK = "task-assistant"

@pytest.fixture
def setup_module(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "setup_telegram.py"
    spec = importlib.util.spec_from_file_location("setup_pairing", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    module.PRIVATE = tmp_path / "private"
    target = {"expected_bot_username": "AssistantTestBot", "agents": {"assistant": {"thread_id": TASK, "title": "Assistant", "file_roots": []}}}
    (tmp_path / "setup-target.json").write_text(json.dumps(target))
    monkeypatch.setattr(module.sys, "argv", ["setup_telegram.py"])
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: "123456:" + "a" * 25)
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda length: "test-pair-code")
    api = AsyncMock()
    api.get_me.return_value = {"username": "AssistantTestBot"}
    api.get_updates.side_effect = [[], [{"update_id": 10, "message": {"text": "/start test-pair-code", "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "is_bot": False}}}]]
    monkeypatch.setattr(module, "TelegramBotApi", lambda token: api)
    return module, api, tmp_path

async def test_pairing_writes_only_registered_task_and_private_permissions(setup_module):
    module, api, root = setup_module
    await module.setup()
    route = json.loads((root / "private/chat-routing.json").read_text())
    assert set(route["agents"]) == {"assistant"}
    assert route["groups"] == {}
    assert "default_private_agent" not in route
    policy = ChatRouting(42, route)
    assert policy.allowed(42, TASK)
    assert not policy.allowed(99, TASK)
    assert not policy.allowed(42, "unregistered-task")
    assert policy.file_roots[TASK] == []
    assert api._call.call_args.args[1]["commands"] == [{"command": "assistant", "description": "Assistant: add your message or attach"}]
    for name in ["bot-token", "config.toml", "state.json", "chat-routing.json"]:
        assert (root / "private" / name).stat().st_mode & 0o077 == 0

async def test_wrong_bot_cannot_pair_or_store_credentials(setup_module):
    module, api, root = setup_module
    api.get_me.return_value = {"username": "OtherBot"}
    await module.setup()
    assert not (root / "private/bot-token").exists()
    assert not (root / "private/config.toml").exists()
    api.get_updates.assert_not_called()
    api.send_message.assert_not_called()

async def test_missing_target_cannot_start_pairing(setup_module):
    module, api, root = setup_module
    (root / "setup-target.json").write_text("{}")
    with pytest.raises(ValueError, match="Register"):
        await module.setup()
    api.get_me.assert_not_called()

async def test_pairing_cannot_replace_existing_routing(setup_module):
    module, api, root = setup_module
    private = root / "private"; private.mkdir()
    old = {"version": 1, "agents": {}, "groups": {}}
    (private / "chat-routing.json").write_text(json.dumps(old))
    with pytest.raises(ValueError, match="routing differs"):
        await module.setup()
    assert json.loads((private / "chat-routing.json").read_text()) == old
    assert not (private / "config.toml").exists()
    api.send_message.assert_not_called()


async def test_pairing_preserves_explicit_private_default(setup_module):
    module, api, root = setup_module
    target_path = root / "setup-target.json"
    target = json.loads(target_path.read_text())
    target["default_private_agent"] = "assistant"
    target_path.write_text(json.dumps(target))
    await module.setup()
    route = json.loads((root / "private/chat-routing.json").read_text())
    assert route["default_private_agent"] == "assistant"
    assert ChatRouting(42, route).default_private_thread(42) == TASK
    assert ChatRouting(42, route).default_private_thread(99) is None


async def test_pairing_preserves_configured_app_and_port(setup_module):
    import tomllib
    module, api, root = setup_module
    path = root / 'setup-target.json'
    target = json.loads(path.read_text())
    target['desktop'] = {'app_path': '/Applications/Example Desktop.app', 'remote_debugging_port': 9237}
    path.write_text(json.dumps(target))
    await module.setup()
    desktop = tomllib.loads((root / 'private/config.toml').read_text())['desktop']
    assert desktop['app_path'] == '/Applications/Example Desktop.app'
    assert desktop['remote_debugging_port'] == 9237


async def test_invalid_desktop_target_is_rejected_before_credentials_or_network(setup_module):
    module, api, root = setup_module
    path = root / 'setup-target.json'
    target = json.loads(path.read_text())
    target['desktop'] = {'app_path': 'relative.app', 'remote_debugging_port': True}
    path.write_text(json.dumps(target))
    with pytest.raises(ValueError, match='absolute'):
        await module.setup()
    api.get_me.assert_not_called()
    assert not (root / 'private/bot-token').exists()
