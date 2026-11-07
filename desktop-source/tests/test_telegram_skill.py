import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from codex_telegram_bridge.state import BridgeState
from codex_telegram_bridge.telegram_skill import bridge_id, install_skill

ROOT = Path(__file__).resolve().parents[2]


def bridge(root):
    shutil.copytree(ROOT / 'skills/telegram', root / 'skills/telegram')
    shutil.copytree(ROOT / 'skills/collaborative-work', root / 'skills/collaborative-work')
    shutil.copy(ROOT / 'send_progress.py', root / 'send_progress.py')
    interpreter = root / 'desktop-source/.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    return root


def test_install_updates_bundle_preserves_other_bridges_and_has_no_credentials(tmp_path):
    first = bridge(tmp_path / 'First Bridge')
    second = bridge(tmp_path / 'Second Bridge')
    target = install_skill(first, tmp_path / 'skills')
    install_skill(second, tmp_path / 'skills')
    install_skill(first, tmp_path / 'skills')
    registry = json.loads((target / 'bridges.json').read_text())
    assert registry == {'version': 1, 'bridges': {
        bridge_id(first): {'root': str(first)}, bridge_id(second): {'root': str(second)}}}
    assert (target / 'SKILL.md').read_bytes() == (ROOT / 'skills/telegram/SKILL.md').read_bytes()
    assert (target / 'bridges.json').stat().st_mode & 0o077 == 0
    collaboration = target.parent / 'collaborative-work'
    for source in (ROOT / 'skills/collaborative-work').rglob('*'):
        if source.is_file():
            installed = collaboration / source.relative_to(ROOT / 'skills/collaborative-work')
            assert installed.read_bytes() == source.read_bytes()
            assert installed.stat().st_mode & 0o077 == 0


def test_missing_collaboration_bundle_does_not_update_existing_installation(tmp_path):
    root = bridge(tmp_path / 'Bridge')
    target = install_skill(root, tmp_path / 'skills')
    before = (target / 'SKILL.md').read_bytes(), (target / 'bridges.json').read_bytes()
    (root / 'skills/telegram/SKILL.md').write_text('Replacement must not be installed')
    (root / 'skills/collaborative-work/references/authority-and-approvals.md').unlink()
    with pytest.raises(ValueError, match='incomplete'):
        install_skill(root, tmp_path / 'skills')
    assert ((target / 'SKILL.md').read_bytes(), (target / 'bridges.json').read_bytes()) == before


def test_unmanaged_collaboration_skill_is_not_overwritten_or_registered(tmp_path):
    root = bridge(tmp_path / 'Bridge')
    collaboration = tmp_path / 'skills/collaborative-work'
    collaboration.mkdir(parents=True)
    (collaboration / 'SKILL.md').write_text('Existing personal playbook')
    with pytest.raises(ValueError, match='unmanaged collaborative-work'):
        install_skill(root, tmp_path / 'skills')
    assert (collaboration / 'SKILL.md').read_text() == 'Existing personal playbook'
    assert not (tmp_path / 'skills/telegram/bridges.json').exists()
    assert not (tmp_path / 'skills/telegram/SKILL.md').exists()


def test_managed_partial_collaboration_install_is_repaired(tmp_path):
    root = bridge(tmp_path / 'Bridge')
    target = install_skill(root, tmp_path / 'skills')
    reference = target.parent / 'collaborative-work/references/authority-and-approvals.md'
    reference.unlink()
    install_skill(root, tmp_path / 'skills')
    assert reference.read_bytes() == (root / 'skills/collaborative-work/references/authority-and-approvals.md').read_bytes()


def test_unrecognized_collaboration_marker_preserves_existing_files(tmp_path):
    root = bridge(tmp_path / 'Bridge')
    target = install_skill(root, tmp_path / 'skills')
    collaboration = target.parent / 'collaborative-work'
    (collaboration / '.bridge-managed.json').write_text('{"managed_by":"someone-else"}')
    before = (target / 'bridges.json').read_bytes()
    with pytest.raises(ValueError, match='Unrecognized collaborative-work'):
        install_skill(root, tmp_path / 'skills')
    assert (target / 'bridges.json').read_bytes() == before


def test_install_does_not_replace_an_unrelated_skill(tmp_path):
    root = bridge(tmp_path / 'Bridge')
    target = tmp_path / 'skills/telegram'; target.mkdir(parents=True)
    (target / 'SKILL.md').write_text('Existing user skill')
    with pytest.raises(ValueError, match='unmanaged'):
        install_skill(root, tmp_path / 'skills')
    assert (target / 'SKILL.md').read_text() == 'Existing user skill'


def test_installed_helper_queues_scoped_idempotent_update_and_reports_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONPATH', str(ROOT / 'desktop-source/src'))
    # Apostrophes, spaces and shell syntax remain literal paths; no shell execution.
    root = bridge(tmp_path / "Owner's $(project)")
    target = install_skill(root, tmp_path / 'skills')
    private = root / 'private'; private.mkdir()
    route = {'version': 1, 'agents': {'editor': {'thread_id': 'task', 'title': 'Editor'}},
             'groups': {'-1001': {'agent': 'editor', 'all_members': True}}}
    (private / 'chat-routing.json').write_text(json.dumps(route))
    state = BridgeState(primary_chat_id=42)
    state.bind_message(-1001, 83, 'task')
    state.save(private / 'state.json')
    text_file = tmp_path / 'Project/Telegram/update.txt'
    text_file.parent.mkdir(parents=True); text_file.write_text("I'll check and report back.")
    payload = {'thread_id': 'task', 'chat_id': -1001, 'reply_to': 83, 'text': text_file.read_text()}
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    receipt = private / 'outbox/receipts' / (key + '.json')
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({'status': 'sent', 'sent_ids': [91]}))
    argv = [sys.executable, str(target / 'scripts/send_update.py'),
            '--bridge', bridge_id(root), '--agent', 'editor', '--chat-id', '-1001',
            '--reply-to', '83', '--text-file', str(text_file)]
    for _ in range(2):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.splitlines()[-1]) == {'status': 'sent', 'sent_ids': [91]}
    requests = list((private / 'outbox/requests').glob('*.json'))
    assert len(requests) == 1 and json.loads(requests[0].read_text()) == payload
    assert requests[0].stat().st_mode & 0o077 == 0
    requests[0].unlink()
    # An explicit origin cannot fall back to a different chat, even with matching IDs.
    argv[argv.index('--chat-id') + 1] = '-1002'
    denied = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    assert denied.returncode != 0 and 'not this agent' in denied.stderr
    assert not list((private / 'outbox/requests').glob('*.json'))
    argv[argv.index('--bridge') + 1] = 'unregistered'
    denied = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    assert denied.returncode != 0 and 'Unknown bridge ID' in denied.stderr
