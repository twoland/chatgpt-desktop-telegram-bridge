from unittest.mock import AsyncMock

import pytest

from codex_telegram_bridge.local_mode import telegram_prompt
from codex_telegram_bridge.state import BridgeState
from codex_telegram_bridge.telegram_skill import bridge_id
from test_chat_isolation import app, message
from test_bridge import make_conversation, make_turn


@pytest.mark.parametrize('chat,user,command,task,alias', [
    (-1001, 101, '/ask', 'task-a', 'editor_a'),
    (42, 42, '/personal', 'task-personal', 'personal'),
])
async def test_routed_request_contains_only_identity_routing_and_user_text(app, chat, user, command, task, alias):
    app._send_thread_input = AsyncMock()
    body = 'Research these options and send me your recommendation.'
    await app._handle_telegram_message(message(f'{command} {body}', chat=chat, user=user, message_id=83))
    prompt = app._send_thread_input.call_args.kwargs['text']
    assert app._send_thread_input.call_args.args[0].thread_id == task
    assert f'Telegram chat ID: {chat}\nTelegram message ID: 83\n' in prompt
    assert f'\nTelegram bridge: {bridge_id(app.config.bridge.state_path.parent.parent)}\nTelegram agent: {alias}\n' in prompt
    assert len(prompt.split('\n\nMessage:\n')[0].splitlines()) == 6
    assert 'reply instructions' not in prompt and 'send_progress' not in prompt
    assert 'acknowledgment' not in prompt and 'promise' not in prompt
    assert prompt.endswith('\n\nMessage:\n' + body)
    # The agent gets behavior from the installed skill, not a generated instruction block.
    app.telegram.send_message.assert_not_called()


async def test_queue_preserves_minimal_metadata_and_original_target_after_restart(app):
    await app._attach_group(-1001, 'task-a')
    app.desktop.threads['task-a'] = make_conversation('task-a', turns=[make_turn('busy', 'inProgress')])
    await app._handle_telegram_message(message('/ask Compare the films', message_id=84))
    saved = BridgeState.load(app.config.bridge.state_path)
    job = saved.threads['task-a'].queued_inputs[0]
    assert 'Telegram agent: editor_a' in job.text
    assert 'Telegram message ID: 84' in job.text
    assert 'acknowledgment' not in job.text
    assert job.text.endswith('Message:\nCompare the films')
    assert job.chat_id == -1001 and job.message_id == 84


def test_legacy_prompt_format_remains_available_for_saved_inputs():
    incoming = message('/ask Quick question')
    assert telegram_prompt(incoming, 'Quick question') == (
        'Telegram sender: "User 101"\nTelegram user ID: 101\n'
        'Telegram chat ID: -1001\nTelegram message ID: 10\n\nMessage:\nQuick question'
    )
