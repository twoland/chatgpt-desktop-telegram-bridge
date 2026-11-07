import fcntl
import importlib.util
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / 'open_desktop.py'
    spec = importlib.util.spec_from_file_location('test_desktop_launcher', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'config.toml').write_text('# Test configuration')
    config = SimpleNamespace(desktop=SimpleNamespace(
        app_path=Path('/Applications/Example Desktop.app'), remote_debugging_port=9237), bridge=SimpleNamespace(
        state_path=private / 'state.json', log_path=private / 'bridge.log'))
    monkeypatch.setattr(module, 'load_config', lambda path: config)
    return module, tmp_path, config


async def test_ready_desktop_still_starts_missing_listener(launcher, monkeypatch):
    module, _, _ = launcher
    monkeypatch.setattr(module, 'endpoint_ready', AsyncMock(return_value=True))
    monkeypatch.setattr(module, 'start_background_bridge', AsyncMock())
    run = Mock()
    monkeypatch.setattr(module.subprocess, 'run', run)
    await module.main()
    module.start_background_bridge.assert_awaited_once()
    run.assert_not_called()


async def test_cold_launch_releases_desktop_before_listener_starts(launcher, monkeypatch):
    module, _, _ = launcher
    held = False

    @asynccontextmanager
    async def hold():
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    async def start_listener():
        assert not held

    monkeypatch.setattr(module, 'DesktopBooking', lambda owner: SimpleNamespace(hold=hold))
    monkeypatch.setattr(module, 'endpoint_ready', AsyncMock(side_effect=[False, True]))
    monkeypatch.setattr(module, 'start_background_bridge', AsyncMock(side_effect=start_listener))
    run = Mock(side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)])
    monkeypatch.setattr(module.subprocess, 'run', run)
    await module.main()
    assert '--remote-debugging-address=127.0.0.1' in run.call_args.args[0]
    assert '--remote-debugging-port=9237' in run.call_args.args[0]
    assert '/Applications/Example Desktop.app' in run.call_args.args[0]
    module.endpoint_ready.assert_awaited_with(9237)
    module.start_background_bridge.assert_awaited_once()
    assert not held


async def test_running_app_without_endpoint_does_not_restart_app(launcher, monkeypatch):
    module, _, _ = launcher
    monkeypatch.setattr(module, 'endpoint_ready', AsyncMock(return_value=False))
    monkeypatch.setattr(module, 'start_background_bridge', AsyncMock())
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(SystemExit):
        await module.main()
    assert run.call_count == 1
    module.start_background_bridge.assert_not_called()


async def test_existing_listener_is_reused_without_spawning(launcher, monkeypatch, capsys):
    module, root, config = launcher
    popen = Mock()
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    with config.bridge.state_path.with_suffix('.run.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write('pid=12345\n')
        lock.flush()
        await module.start_background_bridge(root=root)
    popen.assert_not_called()
    assert 'already running (PID 12345)' in capsys.readouterr().out


async def test_stale_pid_does_not_prevent_background_start(launcher, monkeypatch, capsys):
    module, root, config = launcher
    (root / 'private/bridge.pid').write_text('99999\n')
    lock = config.bridge.state_path.with_suffix('.run.lock').open('a+')

    def spawn(*args, **kwargs):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write('pid=12345\n')
        lock.flush()
        return SimpleNamespace(pid=12345, poll=lambda: None)

    popen = Mock(side_effect=spawn)
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    try:
        await module.start_background_bridge(root=root)
    finally:
        lock.close()
    args, kwargs = popen.call_args
    assert args[0][-2:] == ['--config', str(root / 'private/config.toml')]
    assert kwargs['start_new_session'] is True
    assert kwargs['stdin'] == subprocess.DEVNULL
    assert kwargs['stderr'] == subprocess.STDOUT
    assert kwargs['cwd'] == root
    assert 'started in the background' in capsys.readouterr().out


async def test_simultaneous_launcher_does_not_start_another_listener(launcher, monkeypatch, capsys):
    module, root, config = launcher
    popen = Mock()
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    with config.bridge.state_path.with_suffix('.launcher.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        await module.start_background_bridge(root=root)
    popen.assert_not_called()
    assert 'Another launcher is already starting' in capsys.readouterr().out


async def test_listener_startup_failure_is_reported(launcher, monkeypatch):
    module, root, _ = launcher
    popen = Mock(return_value=SimpleNamespace(pid=12345, poll=lambda: 1))
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    with pytest.raises(RuntimeError, match='exited during startup'):
        await module.start_background_bridge(root=root)


async def test_fresh_unpaired_installation_can_still_open_desktop(launcher, monkeypatch, capsys):
    module, root, _ = launcher
    (root / 'private/config.toml').unlink()
    popen = Mock()
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    await module.start_background_bridge(root=root)
    popen.assert_not_called()
    assert 'Complete Telegram pairing' in capsys.readouterr().out
