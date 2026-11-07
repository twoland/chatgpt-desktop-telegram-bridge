import importlib.util
import json
import sys
from pathlib import Path

import pytest

from codex_telegram_bridge.cli import SingleInstanceError, _hold_single_instance_lock
from codex_telegram_bridge.state import BridgeState


@pytest.fixture
def manager(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "manage_chat_routes.py"
    spec = importlib.util.spec_from_file_location("test_local_route_manager", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    private = tmp_path / "private"
    private.mkdir()
    state = private / "state.json"
    (private / "config.toml").write_text(f'[telegram]\nprimary_chat_id = 42\n[bridge]\nstate_path = "{state}"\n')
    def run(*args):
        monkeypatch.setattr(sys, "argv", ["manage_chat_routes.py", *args])
        module.main()
    return run, private


def test_add_bind_unbind_remove_roundtrip(manager):
    run, private = manager
    run("add", "editor_a", "task-a", "--title", "Editor A")
    run("bind", "-1001", "editor_a", "--allow", "101")
    path = private / "chat-routing.json"
    raw = json.loads(path.read_text())
    assert raw["groups"]["-1001"]["agent"] == "editor_a"
    assert raw["groups"]["-1001"]["allowed_user_ids"] == [101]
    assert raw["groups"]["-1001"]["follow_reply_chains"] is False
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="Unbind"):
        run("remove", "editor_a")
    run("unbind", "-1001")
    run("remove", "editor_a")
    assert "editor_a" not in json.loads(path.read_text())["agents"]


def test_reassignment_cannot_reuse_another_chats_saved_state(manager):
    run, private = manager
    run("add", "editor_a", "task-a", "--title", "Editor A")
    state = BridgeState()
    state.bind_message(42, 10, "task-a")
    state.save(private / "state.json")
    with pytest.raises(ValueError, match="binding"):
        run("bind", "-1001", "editor_a")
    assert json.loads((private / "chat-routing.json").read_text())["groups"] == {}


def test_changes_rejected_while_bridge_is_running(manager):
    run, private = manager
    with _hold_single_instance_lock(private / "state.json"):
        with pytest.raises(SingleInstanceError):
            run("add", "editor_a", "task-a", "--title", "Editor A")
    assert not (private / "chat-routing.json").exists()


def test_project_file_roots_are_scoped_to_one_registered_agent(manager, tmp_path):
    run, private = manager
    project = tmp_path / 'Project'; project.mkdir()
    run('add', 'editor_a', 'task-a', '--title', 'Editor A')
    run('allow-files', 'editor_a', str(project))
    raw = json.loads((private / 'chat-routing.json').read_text())
    assert raw['agents']['editor_a']['file_roots'] == [str(project)]
    assert set(raw['agents']) == {'editor_a'}  # No installation-specific fallback agents.
    with pytest.raises(ValueError, match='Unknown agent'):
        run('allow-files', 'missing', str(project))


def test_reply_chain_toggle_preserves_group_assignment_and_saved_state(manager):
    run, private = manager
    run('add', 'editor_a', 'task-a', '--title', 'Editor A')
    run('bind', '-1001', 'editor_a', '--all-members')
    state = BridgeState()
    state.bind_message(-1001, 77, 'task-a')
    state.save(private / 'state.json')
    before = (private / 'state.json').read_bytes()
    for mode, expected in [('on', True), ('off', False)]:
        run('reply-chains', '-1001', mode)
        group = json.loads((private / 'chat-routing.json').read_text())['groups']['-1001']
        assert group == {'agent': 'editor_a', 'allowed_user_ids': [], 'all_members': True,
                         'follow_reply_chains': expected}
        assert (private / 'state.json').read_bytes() == before
    config_before = (private / 'chat-routing.json').read_bytes()
    with pytest.raises(ValueError, match='not configured'):
        run('reply-chains', '-9999', 'on')
    with _hold_single_instance_lock(private / 'state.json'):
        with pytest.raises(SingleInstanceError):
            run('reply-chains', '-1001', 'on')
    assert (private / 'chat-routing.json').read_bytes() == config_before


def test_list_shows_effective_reply_chain_default_without_rewriting_config(manager, capsys):
    run, private = manager
    raw = {'version': 1, 'agents': {'editor_a': {'thread_id': 'task-a', 'title': 'Editor A'}},
           'groups': {'-1001': {'agent': 'editor_a'}}}
    path = private / 'chat-routing.json'
    path.write_text(json.dumps(raw))
    run('list')
    assert json.loads(capsys.readouterr().out)['groups']['-1001']['follow_reply_chains'] is False
    assert json.loads(path.read_text()) == raw


def test_all_messages_toggle_preserves_other_routes_and_saved_state(manager):
    run, private = manager
    run('add', 'editor_a', 'task-a', '--title', 'Editor A')
    run('add', 'editor_b', 'task-b', '--title', 'Editor B')
    run('bind', '-1001', 'editor_a', '--all-members')
    run('bind', '-1002', 'editor_b', '--allow', '202')
    path = private / 'chat-routing.json'
    original = json.loads(path.read_text())
    state = BridgeState()
    state.bind_message(-1001, 77, 'task-a')
    state.save(private / 'state.json')
    before = (private / 'state.json').read_bytes()
    for mode, expected in [('on', True), ('off', False)]:
        run('all-messages', '-1001', mode)
        raw = json.loads(path.read_text())
        assert raw['groups']['-1001'] == {**original['groups']['-1001'], 'receive_all_messages': expected}
        assert raw['groups']['-1002'] == original['groups']['-1002']
        assert raw['agents'] == original['agents']
        assert (private / 'state.json').read_bytes() == before
    with pytest.raises(ValueError, match='not configured'):
        run('all-messages', '-9999', 'on')
    with _hold_single_instance_lock(private / 'state.json'):
        with pytest.raises(SingleInstanceError):
            run('all-messages', '-1001', 'on')


def test_reply_modes_are_scoped_and_preserve_assignments_and_state(manager):
    run, private = manager
    run('add', 'editor_a', 'task-a', '--title', 'Editor A')
    run('bind', '-1001', 'editor_a', '--all-members')
    state = BridgeState(); state.bind_message(-1001, 10, 'task-a'); state.save(private / 'state.json')
    state_before = (private / 'state.json').read_bytes()
    path = private / 'chat-routing.json'; original = json.loads(path.read_text())
    run('reply-mode', 'First')
    run('reply-mode', 'no', '--chat-id', '-1001')
    assert json.loads(path.read_text()) == {**original, 'reply_mode': 'first', 'reply_modes': {'-1001': 'no'}}
    assert (private / 'state.json').read_bytes() == state_before
    before = path.read_bytes()
    with pytest.raises(ValueError, match='configured group'):
        run('reply-mode', 'all', '--chat-id', '-9999')
    with _hold_single_instance_lock(private / 'state.json'):
        with pytest.raises(SingleInstanceError):
            run('reply-mode', 'all')
    assert path.read_bytes() == before
    state.remove_thread('task-a'); state.save(private / 'state.json')
    run('unbind', '-1001')  # No dangling presentation setting after unbinding.
    assert json.loads(path.read_text())['reply_modes'] == {}
