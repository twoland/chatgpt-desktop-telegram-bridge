"""Open the normal app/profile and start a paired bridge in the background."""
import asyncio
import fcntl
import os
import subprocess
import sys
from pathlib import Path

import httpx
from codex_telegram_bridge.config import DesktopConfig, load_config
from codex_telegram_bridge.desktop_booking import DesktopBooking
from codex_telegram_bridge.desktop_client import is_main_app_target
from codex_telegram_bridge.local_mode import relay_owner

ROOT = Path(__file__).resolve().parent


def listener_status(state_path):
    """Use the instance lock as proof of life, never a possibly stale PID file."""
    with state_path.with_suffix('.run.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            value = lock.read().strip().removeprefix('pid=')
            return True, int(value) if value.isdecimal() else None
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False, None


async def start_background_bridge(*, root=ROOT, timeout=10):
    config_path = root / 'private/config.toml'
    if not config_path.exists():
        print('Desktop connection ready. Complete Telegram pairing before starting the bridge.')
        return
    config = load_config(config_path)
    state_path = config.bridge.state_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize launcher clicks while the listener acquires its own instance lock.
    with state_path.with_suffix('.launcher.lock').open('a+') as launcher_lock:
        try:
            fcntl.flock(launcher_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another launcher is already starting the Telegram bridge.')
            return
        running, pid = listener_status(state_path)
        if running:
            print(f'The Telegram bridge is already running (PID {pid or "unknown"}).')
            return
        runner_log = state_path.parent / 'runner.log'
        with runner_log.open('ab') as output:
            process = subprocess.Popen(
                [sys.executable, '-m', 'codex_telegram_bridge.local_mode', 'run', '--config', str(config_path)],
                cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                # A simultaneous manual start can win the same instance lock.
                running, pid = listener_status(state_path)
                if running:
                    print(f'The Telegram bridge is already running (PID {pid or "unknown"}).')
                    return
                raise RuntimeError(f'Telegram bridge exited during startup ({exit_code}). Check {runner_log}')
            running, pid = listener_status(state_path)
            if running and pid == process.pid:
                print(f'Telegram bridge started in the background (PID {pid}). You can close this terminal.')
                print(f'Connection and delivery log: {config.bridge.log_path}')
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f'Telegram bridge startup is not yet confirmed. Check {runner_log} before starting another listener.')
            await asyncio.sleep(0.1)


async def endpoint_ready(port=9229):
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{port}/json/list")
            return response.status_code == 200 and any(
                is_main_app_target(t) for t in response.json()
            )
    except (httpx.HTTPError, ValueError, TypeError):
        return False


async def main():
    config_path = ROOT / 'private/config.toml'
    desktop = load_config(config_path).desktop if config_path.exists() else DesktopConfig()
    port = desktop.remote_debugging_port
    app = desktop.app_path
    if await endpoint_ready(port):
        print("The desktop connection is already enabled.")
        await start_background_bridge()
        return
    # A bundle's process name can be a full path or differ from the app name.
    # Check its executable directory without matching another app's helpers.
    processes = subprocess.run(["/bin/ps", "-ww", "-axo", "comm="],
                               capture_output=True, text=True, check=True)
    executable_dir = app / "Contents/MacOS"
    if any(Path(line.strip()).parent == executable_dir
           for line in processes.stdout.splitlines() if line.strip()):
        print(f"Quit {app.stem} normally after active tasks finish, then run this launcher again. It will reopen the same app and profile.")
        raise SystemExit(1)
    print("Waiting for the shared desktop to be free...")
    async with DesktopBooking(relay_owner(ROOT)).hold():
        subprocess.run(["/usr/bin/open", "-a", str(app), "--args",
                        "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}"], check=True)
        for _ in range(25):
            if await endpoint_ready(port):
                print("Local desktop endpoint ready.")
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("The app did not expose the local endpoint; live compatibility remains unverified")
    # Release the shared desktop before starting message delivery.
    await start_background_bridge()


if __name__ == "__main__":
    os.umask(0o077)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Cancelled; desktop reservation released.")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
