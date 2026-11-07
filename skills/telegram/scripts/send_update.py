"""Delegate a scoped update to a registered local bridge; never read bot tokens."""
import argparse
import json
from pathlib import Path
import subprocess


def command(args, registry_path):
    registry = json.loads(registry_path.read_text())
    if registry.get("version") != 1:
        raise ValueError("Unsupported Telegram skill registry")
    entry = registry["bridges"].get(args.bridge)
    if not entry:
        raise ValueError("Unknown bridge ID; run install_telegram_skill.py in that bridge")
    root = Path(entry["root"])
    if not root.is_absolute():
        raise ValueError("Bridge path must be absolute")
    return [str(root / "desktop-source/.venv/bin/python"), str(root / "send_progress.py"),
            "--agent", args.agent, "--chat-id", str(args.chat_id),
            "--reply-to", str(args.reply_to), "--text-file", str(args.text_file.resolve()),
            "--wait-seconds", "15"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--reply-to", type=int, required=True)
    parser.add_argument("--text-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        argv = command(args, Path(__file__).resolve().parents[1] / "bridges.json")
        return subprocess.run(argv, check=False).returncode
    except (OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"Telegram update stopped: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
