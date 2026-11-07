"""Hidden token entry and one-time-code pairing; no desktop access."""
import asyncio
import getpass
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

from codex_telegram_bridge.state import BridgeState
from codex_telegram_bridge.chat_routing import ChatRouting
from codex_telegram_bridge.config import DesktopConfig
from codex_telegram_bridge.telegram_api import TelegramBotApi

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"


async def setup():
    os.umask(0o077)
    target_path = ROOT / "setup-target.json"
    target = json.loads(target_path.read_text())
    agents = target.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise ValueError("Register the intended task in setup-target.json before pairing")
    desktop = target.get("desktop", {})
    if not isinstance(desktop, dict):
        raise ValueError("desktop must contain an app_path and remote_debugging_port")
    app_path = Path(desktop.get("app_path", DesktopConfig().app_path)).expanduser()
    port = desktop.get("remote_debugging_port", 9229)
    if not app_path.is_absolute() or app_path.suffix != ".app" or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Choose an absolute macOS .app path and a valid debugging port")
    routing_raw = {"version": 1, "agents": agents, "groups": {}}
    if "default_private_agent" in target:
        routing_raw["default_private_agent"] = target["default_private_agent"]
    ChatRouting(1, routing_raw)  # Validate the explicit registry before token entry.
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    PRIVATE.chmod(0o700)
    token_path = PRIVATE / "bot-token"
    if len(sys.argv) == 2:
        token = Path(sys.argv[1]).expanduser().read_text().strip()
    elif token_path.exists():
        token = token_path.read_text().strip()
    else:
        print("Create a dedicated bot with @BotFather /newbot, then enter its token below.")
        token = getpass.getpass("Bot token (hidden): ").strip()
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Token format is invalid")
    api = TelegramBotApi(token)
    try:
        me = await api.get_me()
        if target_path.exists():
            expected = str(target.get("expected_bot_username") or "").lstrip("@")
            if expected and str(me.get("username") or "").lower() != expected.lower():
                print(f"This token belongs to @{me.get('username', 'unknown')}; expected @{expected}. Nothing was paired.")
                return
        print(f"Bot verified: @{me['username']}")
        # This token is used only with the official Telegram Bot API.
        token_path.write_text(token + "\n")
        token_path.chmod(0o600)
        backlog = await api.get_updates(offset=0, timeout=0)
        offset = max((u["update_id"] + 1 for u in backlog), default=0)
        code = secrets.token_urlsafe(18)
        print(f"\nOpen this one-time link in your Telegram account and press Start:\nhttps://t.me/{me['username']}?start={code}\n")
        print("Waiting up to 10 minutes. Only the private chat presenting this code can pair.")
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            updates = await api.get_updates(offset=offset, timeout=10)
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                sender = msg.get("from") or {}
                if (chat.get("type") == "private" and sender.get("id") == chat.get("id")
                    and not sender.get("is_bot") and msg.get("text") == f"/start {code}"):
                    chat_id = int(chat["id"])
                    state_path = PRIVATE / "state.json"
                    state = BridgeState.load(state_path)
                    if state.primary_chat_id not in {None, chat_id}:
                        raise ValueError("Existing state belongs to a different chat; explicit re-pairing is required")
                    ChatRouting(chat_id, routing_raw).validate_state(state)
                    routing_path = PRIVATE / "chat-routing.json"
                    if routing_path.exists() and json.loads(routing_path.read_text()) != routing_raw:
                        raise ValueError("Existing routing differs; manage it explicitly before pairing")
                    routing_path.write_text(json.dumps(routing_raw, indent=2) + "\n")
                    routing_path.chmod(0o600)
                    state.primary_chat_id = chat_id
                    state.telegram_update_offset = offset
                    state.save(state_path)
                    config = f'''[telegram]
bot_token_file = {json.dumps(str(token_path))}
primary_chat_id = {chat_id}
allowed_chat_ids = [{chat_id}]
queued_reaction = "😴"
seen_reaction = "👀"
done_reaction = "👌"

[desktop]
app_path = {json.dumps(str(app_path))}
remote_debugging_port = {port}
launch_timeout_seconds = 15
send_ack_timeout_seconds = 8
poll_interval_seconds = 2

[bridge]
state_path = {json.dumps(str(state_path))}
allow_first_private_chat = false
log_level = "INFO"
protocol_log_level = "WARNING"
console_log = false
'''
                    config_path = PRIVATE / "config.toml"
                    config_path.write_text(config)
                    config_path.chmod(0o600)
                    await api._call("setMyCommands", {"commands": [
                        {"command": alias, "description": (agent["title"] + ": add your message or attach")[:256]}
                        for alias, agent in agents.items()
                    ]})
                    commands = ", ".join("/" + alias for alias in agents)
                    await api.send_message(chat_id=chat_id, text="Telegram paired. The desktop connection still needs its live check. Commands: " + commands + ".")
                    print(f"Paired @{me['username']} to your private chat. Configuration saved privately.\nNext: run install_telegram_skill.py with this bridge's Python, then have each agent read the installed skill. Complete desktop-board setup from README, configure approved outgoing folders, and open your configured app using open_desktop_for_telegram.command. Select Queue follow-up behavior before sending tasks.")
                    return
        raise TimeoutError("Pairing expired; run setup again for a new code")
    finally:
        await api.close()


if __name__ == "__main__":
    try:
        asyncio.run(setup())
    except KeyboardInterrupt:
        print("Setup cancelled.")
    except Exception as exc:
        # HTTP exception strings can contain Telegram's token-bearing URLs.
        print(f"Setup stopped ({type(exc).__name__}); check the token, network, and private pairing message.", file=sys.stderr)
        raise SystemExit(1)
