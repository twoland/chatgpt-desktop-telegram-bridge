"""Queue an explicit update for an agent's existing Telegram reply chain."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from codex_telegram_bridge.chat_routing import ChatRouting
from codex_telegram_bridge.state import BridgeState


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True, help='Configured alias, e.g. primo')
    parser.add_argument('--reply-to', required=True, type=int, help='Message ID in this task’s Telegram chain')
    parser.add_argument('--chat-id', type=int, help='Originating chat ID; checked against agent routing')
    parser.add_argument('--text-file', required=True, type=Path)
    parser.add_argument('--wait-seconds', type=float, default=0, help='Wait up to 30 seconds for a receipt')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    state = BridgeState.load(root / 'private/state.json')
    routing = ChatRouting.load(state.primary_chat_id, root / 'private/chat-routing.json')
    task = routing.agents[args.agent]['thread_id']
    thread = state.threads[task]
    if args.chat_id is not None and args.chat_id != thread.primary_chat_id:
        raise SystemExit('That chat is not this agent’s Telegram chat')
    if not routing.allowed(thread.primary_chat_id, task) or state.lookup_thread_for_message(thread.primary_chat_id, args.reply_to) != task:
        raise SystemExit('That reply target is not in this agent’s Telegram chat')
    text = args.text_file.read_text().strip()
    if not text:
        raise SystemExit('The update is empty')
    payload = {'thread_id': task, 'chat_id': thread.primary_chat_id, 'reply_to': args.reply_to, 'text': text}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(encoded.encode()).hexdigest()
    folder = root / 'private/outbox/requests'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with NamedTemporaryFile('w', dir=folder, delete=False) as handle:
        handle.write(encoded + '\n')
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(folder / (key + '.json'))
    print('Update queued. Repeating this exact command does not duplicate delivery.')
    receipt = root / 'private/outbox/receipts' / (key + '.json')
    print('Receipt:', receipt, flush=True)
    deadline = time.monotonic() + max(0, min(args.wait_seconds, 30))
    while True:
        status = json.loads(receipt.read_text()) if receipt.exists() else {'status': 'queued'}
        if status.get('status') in {'sent', 'failed', 'blocked', 'uncertain'} or time.monotonic() >= deadline:
            print(json.dumps(status, ensure_ascii=False))
            break
        time.sleep(0.25)


if __name__ == '__main__':
    main()
