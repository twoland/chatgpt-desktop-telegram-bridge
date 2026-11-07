"""Durable, ordered delivery with backoff and explicit uncertainty handling."""
from __future__ import annotations

import logging
import time
import json
import re
from pathlib import Path
from contextvars import ContextVar

from .current_desktop import CurrentDesktopClient, matches_submitted_text, normalized_editor_text
from .desktop_client import DesktopClientError, DesktopBusyError, DesktopDraftConflictError, DesktopQueueModeError, DesktopQueuedReceipt, DesktopSendUnconfirmedError, DesktopTurn
from .formatting import render_markdown_chunks
from .state import QueuedInput
from .telegram_api import SentMessage, TelegramApiError

logger = logging.getLogger(__name__)
OUTPUT = ContextVar('telegram_output', default=None)
MAX_ATTEMPTS = 8


def retry_delay(attempts, retry_after=None):
    return max(min(2 ** min(max(attempts, 1), 6), 60), float(retry_after or 0))


def input_key(job):
    return f'{job.chat_id}:{job.message_id}'


class RetryDeliveryMixin:
    def _reply_source(self, thread, fallback):
        context = OUTPUT.get()
        if context is not None and context[0] == thread.thread_id:
            return context[1].get('reply_to', fallback)
        return fallback

    async def _send_with_reply_mode(self, thread, *, source_message_id, send, unthreaded=False):
        """One request-scoped first reply, shared by updates, chunks and files."""
        chat_id = self._chat_for_thread(thread)
        if chat_id is None:
            return None
        source = self._reply_source(thread, source_message_id)
        key = f'{chat_id}:{source}' if source is not None else None
        mode = self.routing.reply_mode_for(chat_id)
        context = OUTPUT.get()
        current = context[1] if context is not None and context[0] == thread.thread_id else None
        unthreaded = unthreaded or bool(current and current.get('unthreaded'))
        # Upgrade older saved outputs without quoting an already answered input
        # again when an installation first selects First mode.
        if key and key not in thread.reply_deliveries and any(
            entry.get('reply_to') == source and (entry.get('sent_ids') or
                (entry is not current and entry.get('text_status') in {'sending', 'uncertain'}))
            for entry in thread.output_deliveries.values()
        ):
            thread.reply_deliveries[key] = {'status': 'sent_or_uncertain'}
        previous = thread.reply_deliveries.get(key)
        reply_to = source if mode == 'all' or (mode == 'first' and previous is None) else None
        if unthreaded:
            reply_to = None
        reservation = None
        if key and previous is None:
            reservation = {'status': 'sending'}
            thread.reply_deliveries[key] = reservation
        await self._save_state()  # Reserve before an external send or cancellation.
        try:
            sent = await send(reply_to)
        except BaseException as exc:
            if reservation is not None and thread.reply_deliveries.get(key) is reservation:
                if isinstance(exc, TelegramApiError) and not exc.uncertain:
                    del thread.reply_deliveries[key]  # Known rejection: retry may still be first.
                else:
                    reservation['status'] = 'uncertain'
                await self._save_state()
            raise
        if key:
            if sent is not None:
                thread.reply_deliveries[key] = {'status': 'sent', 'message_id': sent.message_id}
            elif reservation is not None and thread.reply_deliveries.get(key) is reservation:
                del thread.reply_deliveries[key]
            await self._save_state()
        return sent

    async def _enqueue_input(self, thread, text, chat_id, message_id):
        key = f'{chat_id}:{message_id}'
        if key in thread.input_receipts:
            return
        if not any(input_key(job) == key for job in thread.queued_inputs):
            thread.queued_inputs.append(QueuedInput(chat_id, message_id, text))
        if message_id not in thread.pending_message_ids:
            thread.pending_message_ids.append(message_id)
        self._record_reaction(thread, chat_id, message_id, 'queued')
        await self._save_state()  # Persist before any desktop mutation.
        await self._flush_reactions(thread)
        await self._dispatch_queued_input(thread)

    async def _dispatch_queued_input(self, thread):
        await self._attempt_queued_input(thread)

    async def _confirm_input(self, thread, job, conversation, turn):
        key = input_key(job)
        thread.input_receipts[key] = {'status': 'confirmed', 'turn_id': turn.turn_id}
        thread.queued_inputs = [item for item in thread.queued_inputs if input_key(item) != key]
        thread.relay_turn_ids = list(dict.fromkeys(thread.relay_turn_ids + [turn.turn_id]))[-300:]
        latest = conversation.latest_turn
        thread.current_turn_id = latest.turn_id if latest and not latest.is_terminal else None
        self._mark_latest_user_input_handled(thread, conversation)
        self._record_reaction(thread, job.chat_id, job.message_id, 'seen')
        await self._save_state()
        await self._flush_reactions(thread)
        logger.info('Desktop delivery confirmed (task=%s, message=%s)', thread.thread_id, key)

    async def _confirm_native_queue(self, thread, job, receipt):
        job.phase = 'native_queued'
        job.native_queue_id = receipt.queue_id
        job.next_attempt_at = 0
        thread.input_receipts[input_key(job)] = {'status': 'queued', 'queue_id': receipt.queue_id}
        await self._save_state()
        logger.info('Native desktop queue confirmed (task=%s, message=%s, queue_id=%s)',
                    thread.thread_id, input_key(job), receipt.queue_id)

    async def _attempt_queued_input(self, thread):
        if not thread.queued_inputs or thread.thread_id in self._threads_with_send_in_flight:
            return
        self._threads_with_send_in_flight.add(thread.thread_id)
        attempt_counted = False
        job = None
        awaiting_queue_mode = False
        awaiting_draft = False
        native = getattr(self.desktop, 'supports_native_queue', False) is True
        try:
            conversation = await self.desktop.read_thread(thread.thread_id)
            if conversation is None:
                raise DesktopClientError('Registered task is unavailable')
            submitted = [p.text for p in thread.queued_inputs if p.phase in {'verify', 'native_queued'}]
            recover = getattr(self.desktop, 'recover_submitted_inputs', None)
            if submitted and callable(recover):
                conversation = await recover(thread.thread_id, submitted)
            for pending in list(thread.queued_inputs):
                if not self.routing.allowed(pending.chat_id, thread.thread_id):
                    raise ValueError('Queued delivery violates chat routing')
                # Reconcile every accepted entry, not just the head. The app may
                # start several turns between polls, or a user may reorder them.
                if pending.attempts or pending.text.startswith('Telegram sender: '):
                    match = next((turn for turn in reversed(conversation.turns)
                        if turn.turn_id not in pending.baseline_turn_ids and matches_submitted_text(turn, pending.text)), None)
                    if match:
                        await self._confirm_input(thread, pending, conversation, match)
            # App-accepted messages no longer block later submissions. They stay
            # persisted until their user message appears in the transcript.
            job = next((p for p in thread.queued_inputs if p.phase != 'native_queued'), None)
            if job is None or job.phase == 'blocked' or job.next_attempt_at > time.time():
                return
            if job.phase == 'verify':
                if native:
                    receipt = CurrentDesktopClient.queued_receipt(
                        await self.desktop.read_native_queue(thread.thread_id), job.text)
                    if receipt:
                        await self._confirm_native_queue(thread, job, receipt)
                        return
                # Send may already have happened. Retry reading the receipt, not
                # the click, even after process cancellation or a restart.
                await self._defer_input(thread, job, 'confirmation pending')
                return
            latest = conversation.latest_turn
            if latest and not latest.is_terminal and not native:
                return  # A busy task is queued work, not a failed attempt.
            if latest and latest.is_terminal and self._terminal_turn_needs_delivery(thread, latest):
                # Capture the preceding reply before a new turn can hide it.
                self._stage_terminal_output(thread, latest)
            job.baseline_turn_ids = [turn.turn_id for turn in conversation.turns if turn.turn_id]
            job.attempts += 1
            attempt_counted = True
            await self._save_state()

            async def checkpoint(phase):
                job.phase = phase
                await self._save_state()

            options = {'queue_if_busy': True} if native else {}
            awaiting_queue_mode = job.phase == 'awaiting_queue_mode'
            awaiting_draft = job.phase == 'awaiting_draft'
            conversation = await self.desktop.send_message(thread.thread_id, job.text,
                recover_draft=job.attempts > 1 or awaiting_queue_mode or awaiting_draft, checkpoint=checkpoint, **options)
            if isinstance(conversation, DesktopQueuedReceipt):
                await self._confirm_native_queue(thread, job, conversation)
                return
            match = next((turn for turn in reversed(conversation.turns)
                if turn.turn_id not in job.baseline_turn_ids and matches_submitted_text(turn, job.text)), None)
            if match is None:
                job.phase = 'verify'
                await self._defer_input(thread, job, 'confirmation pending', increment=False)
                return
            await self._confirm_input(thread, job, conversation, match)
        except DesktopDraftConflictError as exc:
            if attempt_counted:
                job.attempts -= 1
            job.phase = 'awaiting_draft'
            job.next_attempt_at = time.time() + 10
            await self._save_state()
            if not awaiting_draft:
                known = any(normalized_editor_text(p.text) == normalized_editor_text(exc.draft_text)
                            for t in self.state.threads.values() for p in t.queued_inputs)
                logger.warning('Input saved; desktop draft must be resolved (task=%s, message=%s, context=%s, chars=%s, matches_saved_input=%s)',
                               thread.thread_id, input_key(job), exc.context, len(exc.draft_text), known)
        except DesktopQueueModeError:
            if attempt_counted:
                job.attempts -= 1
            job.phase = 'awaiting_queue_mode'
            job.next_attempt_at = time.time() + 10
            await self._save_state()
            if not awaiting_queue_mode:
                logger.warning('Input saved; desktop Follow-up behavior must be Queue (task=%s, message=%s)',
                               thread.thread_id, input_key(job))
        except DesktopBusyError:
            if job is not None:
                if attempt_counted:
                    job.attempts -= 1
                if not job.desktop_wait_since:
                    job.desktop_wait_since = time.time()
                    logger.info('Desktop input waiting for shared access (task=%s, message=%s)',
                                thread.thread_id, input_key(job))
                job.next_attempt_at = time.time() + 2
                await self._save_state()
                if not job.desktop_notice_sent and time.time() - job.desktop_wait_since >= 60:
                    # Reserve before sending: a restart or uncertain Telegram
                    # send must not produce repeated notices. No private names,
                    # active tasks or desktop contents go to another group.
                    job.desktop_notice_sent = True
                    await self._save_state()
                    detail = ('Delivery to the agent has not been confirmed yet.' if job.phase == 'verify'
                              else 'It has not reached the agent yet.')
                    sent = await self._send_with_reply_mode(thread,
                        source_message_id=job.message_id,
                        send=lambda target: self._safe_send_message(chat_id=job.chat_id,
                            text='Your message is saved, but the shared desktop is busy. '
                                 + detail + ' Delivery will retry automatically; no need to resend.',
                            reply_to_message_id=target))
                    if sent:
                        self.state.bind_message_reference(sent.chat_id, sent.message_id, thread.thread_id)
                        await self._save_state()
        except (DesktopClientError, OSError, TimeoutError) as exc:
            if job is None:
                job = next((p for p in thread.queued_inputs if p.phase != 'native_queued'), None)
                if job is None:
                    logger.warning('Cannot reconcile native queue (task=%s, reason=%s)', thread.thread_id, type(exc).__name__)
                    return
            job.desktop_wait_since = 0
            if isinstance(exc, DesktopSendUnconfirmedError):
                job.phase = 'verify'
            await self._defer_input(thread, job, type(exc).__name__, increment=not attempt_counted)
        finally:
            self._threads_with_send_in_flight.discard(thread.thread_id)

    async def _defer_input(self, thread, job, reason, *, increment=True):
        if increment or job.attempts == 0:
            job.attempts += 1
        job.next_attempt_at = time.time() + retry_delay(job.attempts)
        await self._save_state()
        logger.warning('Desktop delivery deferred (task=%s, message=%s, attempt=%s, phase=%s, reason=%s)',
            thread.thread_id, input_key(job), job.attempts, job.phase, reason)
        if job.attempts >= MAX_ATTEMPTS and not job.notice_sent:
            # One notice after the retry window, never one per attempt.
            job.notice_sent = True
            await self._save_state()
            sent = await self._send_with_reply_mode(thread, source_message_id=job.message_id,
                send=lambda target: self._safe_send_message(chat_id=job.chat_id, reply_to_message_id=target,
                    text='Your message is saved, but delivery still needs checking after several retries. Please check the desktop task before resending; it may already have received it.'))
            if sent:
                self.state.bind_message(sent.chat_id, sent.message_id, thread.thread_id)
                await self._save_state()

    async def _start_next_queued_input(self, thread_state):
        # Never pop before acknowledgment: cancellation must not lose the job.
        await self._dispatch_queued_input(thread_state)

    async def _send_thread_text_reply(self, thread_state, *, text, reply_to_message_id):
        context = OUTPUT.get()
        entry = context[1] if context is not None and context[0] == thread_state.thread_id else {}
        if entry.get('text_status') == 'sending':
            entry.update(text_status='uncertain', status='uncertain')
            await self._save_state()
        if entry.get('status') in {'failed', 'uncertain'} or entry.get('next_at', 0) > time.time():
            return None
        if 'chunks' not in entry:
            entry['chunks'] = [{'text': c.text, 'entities': c.entities} for c in render_markdown_chunks(text, self.config.bridge.max_message_chars)]
            entry.setdefault('reply_to', reply_to_message_id)
            entry['sent_ids'] = []
            await self._save_state()
        chat_id = self._chat_for_thread(thread_state)
        if chat_id is None:
            return None
        while len(entry['sent_ids']) < len(entry['chunks']):
            chunk = entry['chunks'][len(entry['sent_ids'])]
            entry['text_status'] = 'sending'
            await self._save_state()
            try:
                sent = await self._send_with_reply_mode(thread_state, source_message_id=entry['reply_to'],
                    unthreaded=entry.get('unthreaded', False),
                    send=lambda target: self.telegram.send_message(chat_id=chat_id, text=chunk['text'],
                        entities=chunk['entities'] or None, reply_to_message_id=target))
            except TelegramApiError as exc:
                entry['attempts'] = entry.get('attempts', 0) + 1
                if self._is_missing_reply_target_error(exc) and entry['attempts'] < MAX_ATTEMPTS:
                    entry['unthreaded'] = True
                    entry['status'] = 'pending'
                elif exc.retryable and entry['attempts'] < MAX_ATTEMPTS:
                    entry['status'] = 'pending'
                else:
                    entry['status'] = 'uncertain' if exc.uncertain else 'failed'
                entry['text_status'] = entry['status']
                entry['next_at'] = time.time() + retry_delay(entry['attempts'], exc.retry_after)
                entry['error'] = str(exc)
                await self._save_state()
                logger.warning('Telegram output deferred (task=%s, attempt=%s, status=%s): %s',
                    thread_state.thread_id, entry['attempts'], entry['status'], exc)
                return None
            entry['sent_ids'].append(sent.message_id)
            entry.update(text_status='pending', attempts=0, next_at=0)
            self.state.bind_message(sent.chat_id, sent.message_id, thread_state.thread_id)
            await self._save_state()
        entry['text_status'] = 'sent'
        await self._save_state()
        return SentMessage(chat_id, entry['sent_ids'][0], {})

    async def _drain_outputs(self, thread):
        for key, entry in list(thread.output_deliveries.items()):
            if entry.get('status') != 'pending' or entry.get('next_at', 0) > time.time():
                continue
            if entry.get('kind') == 'progress':
                token = OUTPUT.set((thread.thread_id, entry))
                try:
                    sent = await self._send_thread_text_reply(thread, text=entry['text'], reply_to_message_id=entry.get('reply_to'))
                    if sent:
                        entry['status'] = 'sent'
                        await self._save_state()
                finally:
                    OUTPUT.reset(token)
            else:
                raw = entry['turn']
                await self._deliver_terminal_turn(thread, DesktopTurn(turn_id=raw['turnId'], status=raw['status'], items=raw.get('items', []), raw=raw))
            if entry.get('kind') == 'progress':
                self._write_progress_receipt(key, entry)
            if entry.get('status') == 'sent':
                await self._mark_pending_messages_done(thread)
                await self._save_state()

    def _write_progress_receipt(self, key, entry):
        if not re.fullmatch(r'progress:[0-9a-f]{64}', key):
            return
        folder = self.config.bridge.state_path.parent / 'outbox/receipts'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / (key.removeprefix('progress:') + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({k: entry[k] for k in ('status', 'sent_ids', 'error', 'next_at') if k in entry}, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(path)

    async def _sync_all_threads_once(self):
        folder = self.config.bridge.state_path.parent / 'outbox/requests'
        for path in sorted(folder.glob('*.json'))[:100]:
            if path.is_symlink() or not re.fullmatch(r'[0-9a-f]{64}', path.stem):
                continue
            try:
                request = json.loads(path.read_text())
                thread = self.state.threads[request['thread_id']]
                if not self.routing.allowed(request['chat_id'], thread.thread_id):
                    raise ValueError('Progress destination violates chat routing')
                if self.state.lookup_thread_for_message(request['chat_id'], request['reply_to']) != thread.thread_id:
                    raise ValueError('Progress reply is not bound to this agent')
                if not isinstance(request['text'], str) or not request['text'].strip():
                    raise ValueError('Progress message is empty')
                key = 'progress:' + path.stem
                entry = thread.output_deliveries.setdefault(key, {'kind': 'progress', 'status': 'pending',
                    'text': request['text'], 'reply_to': request['reply_to']})
                await self._save_state()
                self._write_progress_receipt(key, entry)
                path.unlink()
            except (KeyError, ValueError, OSError) as exc:
                logger.warning('Progress request rejected (%s): %s', path.name, type(exc).__name__)
                path.rename(path.with_suffix('.rejected'))
        await super()._sync_all_threads_once()
