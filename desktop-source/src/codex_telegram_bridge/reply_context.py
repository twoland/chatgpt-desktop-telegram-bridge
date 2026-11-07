"""Bounded, non-recursive reply data supplied with a Telegram update."""
from __future__ import annotations

import json


def reply_context(message):
    original = message.get('reply_to_message')
    if not isinstance(original, dict):
        return ''
    message_id = original.get('message_id')
    if type(message_id) is not int or message_id <= 0:
        return ''
    # reply_to_message is a same-chat reply. External replies are a different
    # Bot API object and must not become an implicit cross-chat history fetch.
    original_chat = original.get('chat')
    if isinstance(original_chat, dict) and original_chat.get('id') != message['chat']['id']:
        return ''
    if (original.get('message_thread_id') is not None
            and message.get('message_thread_id') is not None
            and original['message_thread_id'] != message['message_thread_id']):
        return ''

    data = {}
    sender = original.get('from')
    if isinstance(sender, dict):
        author = {}
        if type(sender.get('id')) is int and sender['id'] > 0:
            author['id'] = sender['id']
        name = ' '.join(sender[k] for k in ('first_name', 'last_name') if isinstance(sender.get(k), str))
        if name.strip():
            author['name'] = ' '.join(name.split())[:256]
        if isinstance(sender.get('username'), str):
            author['username'] = sender['username'][:64]
        if type(sender.get('is_bot')) is bool:
            author['is_bot'] = sender['is_bot']
        if author:
            data['sender'] = author
    sender_chat = original.get('sender_chat')
    if isinstance(sender_chat, dict):
        author = {}
        if type(sender_chat.get('id')) is int:
            author['id'] = sender_chat['id']
        if isinstance(sender_chat.get('title'), str):
            author['title'] = sender_chat['title'][:256]
        if author:
            data['sender_chat'] = author

    # JSON quoting prevents embedded newlines or header-shaped quoted text from
    # becoming a second outer identity block. Do not serialize the whole Message
    # (file IDs, markup, nested replies and unrelated fields are not context).
    for field, limit in [('text', 4096), ('caption', 1024)]:
        value = original.get(field)
        if isinstance(value, str):
            data[field] = value[:limit]
            if len(value) > limit:
                data[field + '_truncated'] = True
    quote = message.get('quote')
    if isinstance(quote, dict) and isinstance(quote.get('text'), str):
        data['selected_quote'] = quote['text'][:1024]
        if len(quote['text']) > 1024:
            data['selected_quote_truncated'] = True
    media = [kind for kind in ('photo', 'document', 'video', 'audio', 'voice', 'animation', 'video_note', 'sticker')
             if original.get(kind)]
    if media:
        data['media'] = media
        for kind in media:
            item = original[kind]
            if isinstance(item, dict) and isinstance(item.get('file_name'), str):
                data['file_name'] = item['file_name'][:256]
                break
    if not any(k in data for k in ('text', 'caption', 'selected_quote', 'media')):
        data['content'] = 'not supplied by Telegram'
    return (f'\nTelegram reply-to message ID: {message_id}'
            + '\nTelegram replied message (quoted data): ' + json.dumps(data, ensure_ascii=False))
