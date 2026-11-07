"""Explicit, verified group upgrades; historical message IDs are never remapped."""
from __future__ import annotations

from copy import deepcopy

from .chat_routing import ChatRouting
from .telegram_api import TelegramApiError


def migration_plan(owner, raw, state, old_chat, new_chat):
    if any(type(chat) is not int or chat >= 0 for chat in (old_chat, new_chat)) or old_chat == new_chat:
        raise ValueError("Migration needs two distinct negative numeric group IDs")
    policy = ChatRouting(owner, raw)
    policy.validate_state(state)
    if old_chat not in policy.groups or new_chat in policy.groups:
        raise ValueError("Old group must be configured and the destination must be unassigned")
    thread_id = policy.group_thread(old_chat)
    thread = state.threads.get(thread_id)
    if thread and thread.queued_inputs:
        raise ValueError("Drain or reconcile this agent's saved input queue before migration")
    updated = deepcopy(raw)
    updated['groups'][str(new_chat)] = updated['groups'].pop(str(old_chat))
    if str(old_chat) in updated.get('reply_modes', {}):
        updated['reply_modes'][str(new_chat)] = updated['reply_modes'].pop(str(old_chat))
    migrated = deepcopy(state)
    archived = {key: value for key, value in migrated.message_bindings.items()
                if key.startswith(f'{old_chat}:')}
    for key in archived:
        del migrated.message_bindings[key]
    held = []
    target = migrated.threads.get(thread_id)
    pending = []
    if target:
        target.primary_chat_id = new_chat
        target.last_chain_message_id = None
        pending = target.pending_message_ids
        target.pending_message_ids = []
        for key, entry in target.output_deliveries.items():
            if entry.get('status') == 'pending':
                entry.update(status='blocked', error='Held after group migration; inspect before resending')
                held.append(key)
        for key, entry in target.reaction_deliveries.items():
            if key.startswith(f'{old_chat}:') and entry.get('status') == 'pending':
                entry.update(status='blocked', error='Historical message ID belongs to the old group')
    ChatRouting(owner, updated).validate_state(migrated)
    return updated, migrated, {'thread_id': thread_id, 'archived_message_bindings': archived,
                              'archived_pending_message_ids': pending, 'held_output_keys': held}


async def verify_migration(api, old_chat, new_chat):
    """Only a fresh Telegram migration parameter establishes group continuity."""
    try:
        await api.send_chat_action(chat_id=old_chat, action='typing')
    except TelegramApiError as exc:
        if exc.migrate_to_chat_id != new_chat or exc.uncertain:
            raise ValueError("Telegram did not verify this exact migration") from None
    else:
        raise ValueError("Telegram did not report a migrated group")
    chat = await api._call('getChat', {'chat_id': new_chat})
    me = await api.get_me()
    member = await api._call('getChatMember', {'chat_id': new_chat, 'user_id': me['id']})
    if (chat.get('id') != new_chat or chat.get('type') != 'supergroup'
            or member.get('status') not in {'administrator', 'creator', 'member'}):
        raise ValueError("Bot membership in the migrated supergroup could not be verified")
    return {'old_chat_id': old_chat, 'new_chat_id': new_chat, 'bot_id': me['id'],
            'type': chat['type'], 'title': chat.get('title'), 'bot_status': member['status'],
            'source': 'Fresh Telegram migrate_to_chat_id response'}
