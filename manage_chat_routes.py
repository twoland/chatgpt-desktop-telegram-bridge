"""Local owner administration for Telegram chat-to-task assignments."""
import argparse
import asyncio
import json
import os
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from codex_telegram_bridge.chat_routing import ChatRouting, DEFAULT_AGENTS, with_reply_mode
from codex_telegram_bridge.cli import _hold_single_instance_lock
from codex_telegram_bridge.state import BridgeState
from codex_telegram_bridge.group_migration import migration_plan, verify_migration

ROOT = Path(__file__).resolve().parent


def migrate_group(owner, raw, state_path, routing_path, old_chat, new_chat):
    # The listener lock is already held. Plan before any network or file change.
    updated, state, report = migration_plan(owner, raw, BridgeState.load(state_path), old_chat, new_chat)

    async def verify():
        from codex_telegram_bridge.config import load_config
        from codex_telegram_bridge.telegram_api import TelegramBotApi
        api = TelegramBotApi(load_config(ROOT / 'private/config.toml').telegram.bot_token)
        try:
            return await verify_migration(api, old_chat, new_chat)
        finally:
            await api.close()

    proof = asyncio.run(verify())
    from uuid import uuid4
    backup = state_path.parent / 'repair-evidence' / ('group-migration-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    before = {routing_path: routing_path.read_bytes(), state_path: state_path.read_bytes()}
    for path, data in before.items():
        target = backup / ('before-' + path.name)
        target.write_bytes(data)
        target.chmod(0o600)
    receipt = {**proof, **report, 'status': 'prepared', 'held_outbox': []}
    receipt_path = backup / 'receipt.json'
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    moved = []
    try:
        # Do not turn old progress requests into new sends or reuse message IDs.
        for path in (state_path.parent / 'outbox/requests').glob('*.json'):
            if path.is_symlink():
                continue
            request = json.loads(path.read_text())
            if request.get('chat_id') != old_chat or request.get('thread_id') != report['thread_id']:
                continue
            held = backup / 'held-outbox'
            held.mkdir(exist_ok=True, mode=0o700)
            destination = held / path.name
            path.replace(destination)
            moved.append((path, destination))
            receipt['held_outbox'].append(path.name)
        state.save(state_path)
        state_path.chmod(0o600)
        from codex_telegram_bridge.chat_routing import save_routing
        save_routing(routing_path, updated)
    except BaseException:
        for path, data in before.items():
            with NamedTemporaryFile('wb', dir=path.parent, delete=False) as handle:
                handle.write(data)
                temporary = Path(handle.name)
            temporary.chmod(0o600)
            temporary.replace(path)
        for original, held in moved:
            held.replace(original)
        receipt['status'] = 'rolled_back'
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        raise
    receipt['status'] = 'complete'
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    receipt_path.chmod(0o600)
    print(f'Migrated existing group {old_chat} to {new_chat}; backups/held historical bindings: {backup}')


def main():
    parser = argparse.ArgumentParser(description="Manage agent/group assignments locally. Stop the bridge before making changes.")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list")
    add = commands.add_parser("add")
    add.add_argument("alias")
    add.add_argument("thread_id")
    add.add_argument("--title", required=True)
    files = commands.add_parser("allow-files", help="Allow one agent to upload files from a project folder")
    files.add_argument("alias")
    files.add_argument("folder", type=Path)
    remove = commands.add_parser("remove")
    remove.add_argument("alias")
    bind = commands.add_parser("bind")
    bind.add_argument("chat_id", type=int)
    bind.add_argument("alias")
    people = bind.add_mutually_exclusive_group()
    people.add_argument("--allow", nargs="+", type=int, default=[])
    people.add_argument("--all-members", action="store_true")
    replies = commands.add_parser("reply-chains", help="Opt a group in or out of forwarding replies to previously routed human messages (default: off)")
    replies.add_argument("chat_id", type=int)
    replies.add_argument("mode", choices=("on", "off"))
    all_messages = commands.add_parser("all-messages", help="Forward every authorized human message in one group (default: off; Telegram must also deliver it)")
    all_messages.add_argument("chat_id", type=int)
    all_messages.add_argument("mode", choices=("on", "off"))
    reply_mode = commands.add_parser("reply-mode", help="Outgoing agent messages: no replies, first only, or all (default)")
    reply_mode.add_argument("mode", type=str.lower, choices=("no", "first", "all"))
    reply_mode.add_argument("--chat-id", type=int, help="Override one configured chat instead of the bridge-wide default")
    unbind = commands.add_parser("unbind")
    unbind.add_argument("chat_id", type=int)
    migration = commands.add_parser('migrate-group', help='Verify and repair a Telegram group-to-supergroup upgrade; old sends are held, never replayed')
    migration.add_argument('old_chat_id', type=int)
    migration.add_argument('new_chat_id', type=int)
    args = parser.parse_args()
    os.umask(0o077)
    # Read identity/configuration only; this tool does not read the bot-token file.
    config = tomllib.loads((ROOT / "private/config.toml").read_text())
    owner = int(config["telegram"]["primary_chat_id"])
    state_path = Path(config["bridge"]["state_path"]).expanduser()
    path = state_path.parent / "chat-routing.json"
    raw = json.loads(path.read_text()) if path.exists() else {"version": 1, "agents": DEFAULT_AGENTS.copy(), "groups": {}}
    if args.action == "list":
        policy = ChatRouting(owner, raw)
        print(json.dumps({**raw, "reply_mode": policy.reply_mode,
                          "reply_modes": {str(chat): mode for chat, mode in policy.reply_modes.items()},
                          "groups": {str(chat): group for chat, group in policy.groups.items()}}, indent=2))
        return
    with _hold_single_instance_lock(state_path):
        if args.action == 'migrate-group':
            migrate_group(owner, raw, state_path, path, args.old_chat_id, args.new_chat_id)
            return
        if args.action == "add":
            if args.alias in raw["agents"]:
                raise ValueError("Alias already exists; remove it explicitly before replacing it")
            raw["agents"][args.alias] = {"thread_id": args.thread_id, "title": args.title}
        elif args.action == "allow-files":
            if args.alias not in raw["agents"]:
                raise ValueError("Unknown agent alias")
            folder = args.folder.expanduser().resolve(strict=True)
            if not folder.is_dir() or folder == Path("/"):
                raise ValueError("Choose a project folder")
            roots = raw["agents"][args.alias].setdefault("file_roots", [])
            if str(folder) not in roots:
                roots.append(str(folder))
        elif args.action == "remove":
            if any(group["agent"] == args.alias for group in raw["groups"].values()):
                raise ValueError("Unbind this agent's group before removing it")
            if args.alias not in raw["agents"]:
                raise ValueError("Unknown agent alias")
            del raw["agents"][args.alias]
        elif args.action == "bind":
            key = str(args.chat_id)
            if key in raw["groups"]:
                raise ValueError("Group already assigned; unbind it explicitly before replacing it")
            raw["groups"][key] = {"agent": args.alias, "allowed_user_ids": args.allow,
                                  "all_members": args.all_members, "follow_reply_chains": False}
        elif args.action == "reply-chains":
            key = str(args.chat_id)
            if key not in raw["groups"]:
                raise ValueError("Group is not configured")
            raw["groups"][key]["follow_reply_chains"] = args.mode == "on"
        elif args.action == "all-messages":
            key = str(args.chat_id)
            if key not in raw["groups"]:
                raise ValueError("Group is not configured")
            raw["groups"][key]["receive_all_messages"] = args.mode == "on"
        elif args.action == "reply-mode":
            raw = with_reply_mode(raw, args.mode, args.chat_id)
        elif args.action == "unbind":
            if str(args.chat_id) not in raw["groups"]:
                raise ValueError("Group is not configured")
            del raw["groups"][str(args.chat_id)]
            raw.get("reply_modes", {}).pop(str(args.chat_id), None)
        policy = ChatRouting(owner, raw)
        policy.validate_state(BridgeState.load(state_path))
        with NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
            json.dump(raw, handle, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(path)
    print("Saved group routing. Restart the bridge to load it.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise SystemExit(str(exc))
