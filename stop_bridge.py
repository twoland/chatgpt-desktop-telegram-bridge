"""Stop only the recorded bridge process after checking its command identity."""
import os
import signal
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
pid_path = root / "private/bridge.pid"
if not pid_path.exists():
    raise SystemExit("No recorded bridge process. Close its terminal if started manually.")
pid = int(pid_path.read_text().strip())
result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
if result.returncode != 0 or not result.stdout.strip():
    raise SystemExit("The recorded bridge process is no longer running.")
if "codex_telegram_bridge.local_mode run" not in result.stdout or str(root / "private/config.toml") not in result.stdout:
    raise SystemExit("PID identity differs; refusing to stop another process.")
os.kill(pid, signal.SIGTERM)
print("Bridge shutdown requested. It will release any desktop booking before exiting.")
