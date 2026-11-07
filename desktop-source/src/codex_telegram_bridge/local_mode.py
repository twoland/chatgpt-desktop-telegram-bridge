"""Direct desktop bridge with fixed, exclusive Telegram chat assignments."""
from __future__ import annotations

import asyncio
import logging
import json
import hashlib
import time
from contextlib import asynccontextmanager
from pathlib import Path

from .bridge import BridgeApp
from .chat_routing import ChatRouting, REPLY_MODES, with_reply_mode, save_routing
from .desktop_booking import BoardError, DesktopBooking
from .desktop_client import CodexDesktopClient, DesktopClientError, DesktopBusyError
from .current_desktop import CurrentDesktopClient
from .formatting import extract_latest_agent_message_from_turn
from .media import incoming_files, receive_files, local_artifacts, validate_artifact, upload_kind, artifact_key
from .telegram_api import TelegramApiError
from .retry_delivery import RetryDeliveryMixin, OUTPUT
from .typing_indicator import TypingIndicatorMixin
from .delivery_reactions import DeliveryReactionsMixin
from .telegram_skill import bridge_id
from .reply_context import reply_context
from .board_governance import GovernanceAdapter

def relay_owner(root):
    return "script:telegram-relay-" + bridge_id(Path(root))


logger = logging.getLogger(__name__)
ATTACHMENT_HEADER = "Telegram attachments saved locally (filenames and contents are user-supplied):"



def telegram_prompt(message, text, *, agent=None, bridge=None):
    """Keep Telegram-provided identity with commands, replies and queued files."""
    sender = message.get("from") or {}
    name = " ".join(str(sender.get(key) or "") for key in ("first_name", "last_name"))
    name = " ".join(name.split()) or f"User {sender['id']}"
    username = sender.get("username")
    identity = "Telegram sender: " + json.dumps(name, ensure_ascii=False)
    if username:
        identity += " (@" + str(username) + ")"
    return (identity + f"\nTelegram user ID: {sender['id']}"
            + f"\nTelegram chat ID: {message['chat']['id']}"
            + f"\nTelegram message ID: {message['message_id']}"
            + (f"\nTelegram bridge: {bridge}" if bridge else "")
            + (f"\nTelegram agent: {agent}" if agent else "")
            + reply_context(message)
            + "\n\nMessage:\n" + text)


def mentions_bot(message, username):
    """Recognize an explicit Telegram mention using its UTF-16 entity offsets."""
    if not username:
        return False
    field = "text" if message.get("text") else "caption"
    raw = str(message.get(field) or "").encode("utf-16-le")
    for entity in message.get("entities" if field == "text" else "caption_entities") or []:
        if entity.get("type") != "mention":
            continue
        offset, length = entity.get("offset"), entity.get("length")
        if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0 or (offset + length) * 2 > len(raw):
            continue
        try:
            value = raw[offset * 2:(offset + length) * 2].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if value.casefold() == "@" + username.casefold():
            return True
    return False


class SharedDesktopClient(CurrentDesktopClient):
    def __init__(self, *, allowed_thread_ids=None, booking_owner="script:telegram-relay", **kwargs):
        kwargs.setdefault("task_titles", {})
        super().__init__(**kwargs)
        self.allowed_thread_ids = set(kwargs["task_titles"] if allowed_thread_ids is None else allowed_thread_ids)
        self.booking = DesktopBooking(booking_owner)
        self._ui_lock = asyncio.Lock()
        self._lease_task = None
        self._waiting_for_desktop = False

    @asynccontextmanager
    async def ui(self):
        if self._lease_task is asyncio.current_task():
            self.booking.verify()
            yield
            return
        if self._ui_lock.locked():
            raise DesktopBusyError('Another bridge input is using the desktop')
        async with self._ui_lock:
            try:
                if not self.booking.try_claim():
                    self._waiting_for_desktop = True
                    raise DesktopBusyError('Waiting for the shared desktop reservation')
                self._waiting_for_desktop = False
                try:
                    self._lease_task = asyncio.current_task()
                    async with asyncio.timeout(55):
                        yield
                finally:
                    self._lease_task = None
                    self.booking.release()
            except (BoardError, TimeoutError, OSError) as exc:
                raise DesktopClientError(f"Desktop booking or UI operation stopped: {type(exc).__name__}. Check the board and task before retrying.") from exc

    def check_thread(self, thread_id):
        if thread_id not in self.allowed_thread_ids:
            raise DesktopClientError("This task is not enabled in the relay")

    async def _call_cdp(self, *args, **kwargs):
        if self._lease_task is asyncio.current_task():
            self.booking.verify()
        return await super()._call_cdp(*args, **kwargs)

    async def list_threads(self):
        return [item for item in await super().list_threads() if item.thread_id in self.allowed_thread_ids]

    async def read_thread(self, thread_id):
        self.check_thread(thread_id)
        return await super().read_thread(thread_id)

    async def activate_thread(self, thread_id):
        self.check_thread(thread_id)
        async with self.ui():
            return await super().activate_thread(thread_id)

    async def send_message(self, thread_id, text, **kwargs):
        self.check_thread(thread_id)
        async with self.ui():
            return await super().send_message(thread_id, text, **kwargs)

    async def read_native_queue(self, thread_id):
        self.check_thread(thread_id)
        async with self.ui():
            return await super().read_native_queue(thread_id)

    async def close(self):
        try:
            await super().close()
        finally:
            if self._waiting_for_desktop:
                self.booking.cancel_reservation()
                self._waiting_for_desktop = False

    async def send_existing_draft(self, thread_id, text):
        self.check_thread(thread_id)
        async with self.ui():
            return await super().send_existing_draft(thread_id, text)

    async def start_new_thread(self, *args, **kwargs):
        raise DesktopClientError("New tasks are disabled; register an existing task explicitly")

    async def capture_screenshot(self, path: Path):
        async with self.ui():
            return await super().capture_screenshot(path)

    async def click_approval_action(self, *args, **kwargs):
        # Upstream clicks generic labels rather than the exact request ID.
        # Keep approvals in the native app until an exact request adapter is validated.
        raise DesktopClientError("Review and answer this approval in the desktop app")


class LocalBridgeApp(DeliveryReactionsMixin, TypingIndicatorMixin, RetryDeliveryMixin, BridgeApp):
    def __init__(self, config, state):
        if config.bridge.allow_first_private_chat or not config.telegram.primary_chat_id:
            raise ValueError("Pair an exact private Telegram chat before starting")
        if config.telegram.allowed_chat_ids != [config.telegram.primary_chat_id]:
            raise ValueError("The allowlist must contain only the paired private chat")
        self.routing = ChatRouting.load(config.telegram.primary_chat_id, config.bridge.state_path.parent / "chat-routing.json")
        self.routing.validate_state(state)
        if not self.routing.agents:
            raise ValueError("Register at least one existing task in private/chat-routing.json before starting")
        self._bot_username = ""
        desktop = SharedDesktopClient(
            booking_owner=relay_owner(config.bridge.state_path.parent.parent),
            allowed_thread_ids=self.routing.titles,
            task_titles=self.routing.titles,
            app_path=config.desktop.app_path,
            user_data_dir=config.desktop.user_data_dir,
            remote_debugging_port=config.desktop.remote_debugging_port,
            launch_timeout_seconds=config.desktop.launch_timeout_seconds,
            send_ack_timeout_seconds=config.desktop.send_ack_timeout_seconds,
            poll_interval_seconds=config.desktop.poll_interval_seconds,
        )
        super().__init__(config, state, desktop=desktop)
        self._input_delivery_tasks = {}
        self.governance = GovernanceAdapter.load(
            config.bridge.state_path.parent / "board-governance.json",
            owner=self.routing.owner, bridge_id=bridge_id(config.bridge.state_path.parent.parent),
            send=self.telegram.send_message,
        )

    async def run(self):
        task = None
        if self.governance is not None:
            task = asyncio.create_task(self._governance_loop(), name="board-governance")
            self._tasks.add(task)
        try:
            await super().run()
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._tasks.discard(task)
                self.governance.close()

    async def close(self):
        try:
            await super().close()
        finally:
            if self.governance is not None:
                self.governance.close()

    async def _governance_loop(self):
        while not self._shutdown.is_set():
            if self._bot_id is not None:
                try:
                    await self.governance.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error("Board governance sync held; inspect its private journal.")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    async def _handle_telegram_message(self, message):
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        text = str(message.get("text") or message.get("caption") or "").strip()
        attachments = incoming_files(message)
        parts = text.split(maxsplit=1)
        command, argument = (parts + [""])[:2] if parts else ("", "")
        route, separator, username = command.lower().removeprefix("/").partition("@") if command.startswith("/") else ("", "", "")
        if separator and (not self._bot_username or username != self._bot_username):
            return
        # The paired owner can discover a group's numeric ID before it is enabled.
        if (route == "chatid" and sender.get("id") == self.routing.owner and
            not sender.get("is_bot") and not message.get("sender_chat") and
            chat.get("type") in {"private", "group", "supergroup"}):
            await self._safe_send_message(chat_id=chat_id, text=f"Telegram chat ID: {chat_id}", reply_to_message_id=message["message_id"])
            return
        if not self.routing.accepts_sender(chat, sender, anonymous=bool(message.get("sender_chat"))):
            return
        if self.governance is not None and self.governance.accept_message(message):
            return
        available = self.routing.agents_for(chat_id)
        message_id = int(message["message_id"])
        if route == "replymode":
            if argument and sender.get("id") != self.routing.owner:
                await self._safe_send_message(chat_id=chat_id,
                    text="Only the paired owner can change reply modes.")
                return
            mode = argument.strip().lower()
            if mode and mode not in REPLY_MODES:
                await self._safe_send_message(chat_id=chat_id, text="Use /replymode no, /replymode first or /replymode all.")
                return
            if mode:
                path = self.config.bridge.state_path.parent / "chat-routing.json"
                raw = json.loads(path.read_text())
                updated = with_reply_mode(raw, mode, chat_id)
                routing = ChatRouting(self.routing.owner, updated)
                routing.validate_state(self.state)
                save_routing(path, updated)
                self.routing = routing
            await self._safe_send_message(chat_id=chat_id,
                text=f"Reply mode: {self.routing.reply_mode_for(chat_id).title()}. No = plain messages; First = only the first response to each request is a reply; All = every response is a reply.")
            return
        if route in {"start", "help", "agents", "agent", "status"}:
            names = ", ".join(f"/{alias}: {item['title']}" for alias, item in available.items()) or "No agents assigned here"
            if chat.get("type") == "private" and self.routing.default_private_thread(chat_id):
                help_text = names + f". Just send a message here to /{self.routing.default_private_agent}; no command or reply is needed. /detach stops syncing, not the running task."
            elif self.routing.groups.get(chat_id, {}).get("receive_all_messages", False):
                help_text = names + ". All human messages and supported attachments in this group are routed to its assigned agent, without a mention. Telegram must deliver them to the bot. Commands addressed to other bots are ignored."
            else:
                help_text = names + ". Use /attach, /ask <message> in a group, or reply to your agent. Send attachments as replies, or put /ask in their caption. /detach stops syncing, not the running task."
            help_text += f" Reply mode: {self.routing.reply_mode_for(chat_id).title()}; /replymode shows it (owner can set no, first or all)."
            await self._safe_send_message(chat_id=chat_id, text=help_text, reply_to_message_id=message_id)
            return
        if route in {"attach", "detach"}:
            thread_id = self.routing.resolve(chat_id, argument) if argument else self.routing.group_thread(chat_id)
            reply_id = (message.get("reply_to_message") or {}).get("message_id")
            if not thread_id and not argument and reply_id is not None:
                thread_id = self.state.lookup_thread_for_message(chat_id, int(reply_id))
            if thread_id is None and not argument and route == "attach":
                await self._handle_telegram_message({**message, "text": "/agents"})
                return
            if not self.routing.allowed(chat_id, thread_id):
                await self._deny(chat_id, message_id)
                return
            if route == "attach":
                await self._handle_attach_command(chat_id=chat_id, message_id=message_id, thread_id=thread_id)
            else:
                await super()._handle_detach_command(chat_id=chat_id, message_id=message_id, reply_to_message_id=None, thread_id=thread_id)
            return
        thread_id = None
        if route in self.routing.agents:
            thread_id = self.routing.resolve(chat_id, route)
            if thread_id is None:
                await self._deny(chat_id, message_id)
                return
            if not argument.strip() and not attachments:
                await self._handle_attach_command(chat_id=chat_id, message_id=message_id, thread_id=thread_id)
                return
            text = argument
        elif route == "ask":
            thread_id = self.routing.group_thread(chat_id)
            text = argument
        elif command.startswith("/"):
            await self._deny(chat_id, message_id)
            return
        else:
            reply = message.get("reply_to_message") or {}
            reply_id = reply.get("message_id")
            reply_sender = reply.get("from") or {}
            replies_to_bot = (self._bot_id is not None
                              and reply_sender.get("id") == self._bot_id
                              and reply_sender.get("is_bot") is True)
            follows_human_reply = (
                self.routing.groups.get(chat_id, {}).get("follow_reply_chains", False)
                and type(reply_sender.get("id")) is int and reply_sender["id"] > 0
                and reply_sender.get("is_bot") is False)
            # Bindings also include incoming human messages for delivery receipts.
            # Following those reply chains requires an explicit per-group opt-in;
            # it does not accept unbound human replies or messages from other bots.
            if reply_id is not None and (chat.get("type") == "private" or replies_to_bot or follows_human_reply):
                thread_id = self.state.lookup_thread_for_message(chat_id, int(reply_id))
                # Older error/help notices may have no saved binding. Telegram's
                # sender ID proves the replied-to message came from this bot;
                # the configured group still selects exactly one allowed task.
                if thread_id is None and replies_to_bot:
                    thread_id = self.routing.group_thread(chat_id)
            if thread_id is None and mentions_bot(message, self._bot_username):
                thread_id = self.routing.group_thread(chat_id)
            # This independent per-group opt-in accepts plain messages and
            # attachments, including replies outside a previously known chain.
            # Sender authorization and explicit command handling still apply.
            if thread_id is None and self.routing.groups.get(chat_id, {}).get("receive_all_messages", False):
                thread_id = self.routing.group_thread(chat_id)
            # Ordinary private messages route only through an explicit opt-in.
            # Existing commands and reply bindings take precedence over the default.
            if thread_id is None and chat.get("type") == "private":
                thread_id = self.routing.default_private_thread(chat_id)
        if attachments and not text:
            text = "Please review the attached file."
        if not thread_id or not text:
            if chat_id == self.routing.owner:
                await self._handle_telegram_message({**message, "text": "/help"})
            return
        if not self.routing.allowed(chat_id, thread_id):
            await self._deny(chat_id, message_id)
            return
        existing = self.state.threads.get(thread_id)
        if existing and (f'{chat_id}:{message_id}' in existing.input_receipts or any(
            job.chat_id == chat_id and job.message_id == message_id for job in existing.queued_inputs)):
            return  # Telegram can replay an update after a process interruption.
        if attachments:
            try:
                paths = await receive_files(self.telegram, base=self.config.bridge.state_path.parent / "media/incoming",
                    thread_id=thread_id, chat_id=chat_id, message_id=message_id, attachments=attachments)
            except (TelegramApiError, OSError, ValueError) as exc:
                detail = str(exc) if isinstance(exc, TelegramApiError) else "The attachment could not be saved locally"
                await self._safe_send_message(chat_id=chat_id, text=f"Attachment not delivered: {detail}. Send a smaller file or a shared link.", reply_to_message_id=message_id)
                return
            files = "\n".join("- " + json.dumps(str(path), ensure_ascii=False) for path in paths)
            text += "\n\n" + ATTACHMENT_HEADER + "\n" + files
        text = self._telegram_prompt(message, text, thread_id)
        if chat_id in self.routing.groups and thread_id not in self.state.threads:
            if not await self._attach_group(chat_id, thread_id):
                await self._safe_send_message(chat_id=chat_id, text="Your agent is not currently available in the desktop app.", reply_to_message_id=message_id)
                return
        self.state.bind_message(chat_id, message_id, thread_id)
        await self._save_state()
        # Use the durable sender for busy tasks too. The upstream busy shortcut
        # only appends to a bridge queue, which is invisible to goal continuation.
        thread = self.state.get_or_create_thread(thread_id)
        thread.primary_chat_id = chat_id
        await self._send_thread_input(thread, text=text, source_chat_id=chat_id,
                                     source_message_id=message_id, bind_on_success=True)

    def _telegram_prompt(self, message, text, thread_id):
        alias = next(alias for alias, agent in self.routing.agents.items() if agent['thread_id'] == thread_id)
        return telegram_prompt(message, text, agent=alias,
                               bridge=bridge_id(self.config.bridge.state_path.parent.parent))

    async def _deny(self, chat_id, message_id):
        await self._safe_send_message(chat_id=chat_id, text="That action or agent is not available in this chat.", reply_to_message_id=message_id)

    async def _attach_group(self, chat_id, thread_id):
        if not self.routing.allowed(chat_id, thread_id):
            return False
        conversation = await self.desktop.read_thread(thread_id)
        if conversation is None:
            return False
        thread = self.state.get_or_create_thread(thread_id)
        thread.primary_chat_id = chat_id
        turn = conversation.latest_turn
        thread.current_turn_id = turn.turn_id if turn and not turn.is_terminal else None
        self._mark_latest_user_input_handled(thread, conversation)
        await self._save_state()
        return True

    async def _handle_attach_command(self, *, chat_id, message_id, thread_id):
        if not self.routing.allowed(chat_id, thread_id):
            await self._deny(chat_id, message_id)
            return
        if chat_id not in self.routing.groups:
            await super()._handle_attach_command(chat_id=chat_id, message_id=message_id, thread_id=thread_id)
            return
        if not await self._attach_group(chat_id, thread_id):
            await self._safe_send_message(chat_id=chat_id, text="Your agent is not currently available in the desktop app.", reply_to_message_id=message_id)
            return
        self.state.bind_message(chat_id, message_id, thread_id)
        sent = await self._safe_send_message(chat_id=chat_id, text=f"Connected to {self.routing.titles[thread_id]}. Use /ask <message> or reply here. Earlier conversation history is not copied into the group.", reply_to_message_id=message_id)
        if sent:
            self.state.bind_message(chat_id, sent.message_id, thread_id)
        await self._save_state()

    async def _handle_callback_query(self, query):
        # No broad upstream pickers or generic approval callbacks in isolated chats.
        message = query.get("message") or {}
        if self.routing.accepts_sender(message.get("chat") or {}, query.get("from") or {}):
            await self._safe_answer_callback_query(str(query["id"]), text="Use /agents and /attach. This button is no longer active.")
        else:
            await self._safe_answer_callback_query(str(query["id"]), text="Unavailable")

    def _chat_for_thread(self, thread_state):
        chat_id = thread_state.primary_chat_id
        return chat_id if self.routing.allowed(chat_id, thread_state.thread_id) else None

    async def _send_thread_input(self, thread_state, *, text, source_chat_id, source_message_id, bind_on_success=False):
        if not self.routing.allowed(source_chat_id, thread_state.thread_id):
            await self._deny(source_chat_id, source_message_id)
            return
        if thread_state.primary_chat_id != source_chat_id:
            raise ValueError("Cannot send through a task belonging to another chat")
        if bind_on_success:
            self.state.bind_message(source_chat_id, source_message_id, thread_state.thread_id)
        await self._enqueue_input(thread_state, text, source_chat_id, source_message_id)

    async def _dispatch_queued_input(self, thread_state):
        """Desktop submission must not occupy Telegram intake or output sync.

        There is one bounded attempt per task. SharedDesktopClient still owns
        the single UI lease, and durable phase checkpoints still prevent a
        cancelled/uncertain click from being submitted twice.
        """
        thread_id = thread_state.thread_id
        if (self._shutdown.is_set() or not thread_state.queued_inputs
                or thread_id in self._input_delivery_tasks):
            return
        task = asyncio.create_task(self._attempt_queued_input(thread_state),
                                   name=f'desktop-input:{thread_id}')
        self._input_delivery_tasks[thread_id] = task
        self._tasks.add(task)

        def finished(completed):
            self._input_delivery_tasks.pop(thread_id, None)
            self._tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logger.error('Desktop input worker failed (task=%s, reason=%s); saved input retained',
                             thread_id, type(completed.exception()).__name__)

        task.add_done_callback(finished)

    async def _sync_latest_user_input(self, thread_state, conversation):
        if thread_state.thread_id in self._threads_with_send_in_flight:
            return False
        # Inputs belong to their originating surface. Telegram already has its
        # own inputs; desktop inputs must never be copied to the bot chat.
        key, _ = self._latest_user_input_key_and_text(conversation)
        if key is None or key == thread_state.last_handled_user_input_key:
            return False
        thread_state.last_handled_user_input_key = key
        return True

    def _is_telegram_turn(self, thread_state, turn_id):
        if not turn_id:
            return False
        return turn_id in thread_state.relay_turn_ids or any(
            key.startswith(f'{thread_state.primary_chat_id}:')
            and receipt.get('status') == 'confirmed'
            and receipt.get('turn_id') == turn_id
            for key, receipt in thread_state.input_receipts.items()
        )

    def _terminal_turn_needs_delivery(self, thread_state, turn):
        entry = thread_state.output_deliveries.get(turn.turn_id)
        if entry and (entry.get('status') != 'pending' or entry.get('next_at', 0) > time.time()):
            return False
        if not self._is_telegram_turn(thread_state, turn.turn_id):
            return False
        if super()._terminal_turn_needs_delivery(thread_state, turn):
            return True
        item_id, text = extract_latest_agent_message_from_turn(turn.raw)
        if not text or not item_id:
            return False
        for artifact in local_artifacts(text):
            receipt = thread_state.media_deliveries.get(artifact_key(turn.turn_id, item_id, artifact))
            if not receipt or receipt.get("status") == "uploading" or (receipt.get("status") != "sent" and not receipt.get("notice_sent")):
                return True
        return False

    def _stage_terminal_output(self, thread_state, turn):
        entry = thread_state.output_deliveries.setdefault(turn.turn_id,
            {'status': 'pending', 'turn': {**turn.raw, 'turnId': turn.turn_id, 'status': turn.status, 'items': turn.items}})
        for key, receipt in thread_state.input_receipts.items():
            if receipt.get('turn_id') == turn.turn_id and key.startswith(f'{thread_state.primary_chat_id}:'):
                entry.setdefault('reply_to', int(key.split(':', 1)[1]))
                break
        return entry

    async def _deliver_terminal_turn(self, thread_state, turn):
        if not self._is_telegram_turn(thread_state, turn.turn_id):
            entry = thread_state.output_deliveries.get(turn.turn_id)
            if entry and entry.get('status') == 'pending':
                entry.update(status='suppressed', error='Turn did not originate from Telegram')
                await self._save_state()
            return False
        entry = self._stage_terminal_output(thread_state, turn)
        if entry.get('kind') == 'progress':
            return entry['status'] == 'sent'
        if entry['status'] != 'pending' or entry.get('next_at', 0) > time.time():
            return entry['status'] == 'sent'
        await self._save_state()
        token = OUTPUT.set((thread_state.thread_id, entry))
        try:
            done = await self._deliver_terminal_turn_once(thread_state, turn)
            if done:
                entry['status'] = 'sent'
                await self._save_state()
            return done
        finally:
            OUTPUT.reset(token)

    async def _deliver_terminal_turn_once(self, thread_state, turn):
        if not self._is_telegram_turn(thread_state, turn.turn_id):
            return False
        if self._chat_for_thread(thread_state) is None:
            return False
        if super()._terminal_turn_needs_delivery(thread_state, turn):
            if not await super()._deliver_terminal_turn(thread_state, turn):
                return False
            await self._save_state()
        item_id, text = extract_latest_agent_message_from_turn(turn.raw)
        if not text or not item_id:
            return True
        artifacts = local_artifacts(text)
        if not artifacts:
            return True
        roots = list(self.routing.file_roots.get(thread_state.thread_id, []))
        task_key = hashlib.sha256(thread_state.thread_id.encode()).hexdigest()[:24]
        roots.append(self.config.bridge.state_path.parent / "media/incoming" / task_key / str(thread_state.primary_chat_id))
        complete = True
        for artifact in artifacts:
            key = artifact_key(turn.turn_id, item_id, artifact)
            receipt = thread_state.media_deliveries.get(key)
            if receipt and receipt.get("status") == "sent":
                continue
            if receipt and receipt.get("status") == "uploading":
                receipt.update(status="unconfirmed", error="An earlier upload was interrupted; it will not be sent twice automatically")
                await self._save_state()
            if receipt is None:
                receipt = {"status": "uploading", "name": artifact.path.name, "notice_sent": False}
                thread_state.media_deliveries[key] = receipt
                # Persist before contacting Telegram; a crash must not duplicate a file.
                await self._save_state()
                try:
                    path = validate_artifact(artifact, roots)
                    sent = await self._send_with_reply_mode(thread_state,
                        source_message_id=thread_state.last_chain_message_id,
                        send=lambda target: self.telegram.send_file(chat_id=thread_state.primary_chat_id, path=path,
                            kind=upload_kind(artifact, path), reply_to_message_id=target))
                    self.state.bind_message(sent.chat_id, sent.message_id, thread_state.thread_id)
                    receipt.update(status="sent", message_id=sent.message_id)
                except (TelegramApiError, OSError, ValueError) as exc:
                    detail = str(exc) if isinstance(exc, (TelegramApiError, ValueError)) else "File is missing or cannot be read"
                    receipt.update(status="failed", error=detail)
                await self._save_state()
            if receipt.get("status") != "sent" and not receipt.get("notice_sent"):
                sent = await self._send_with_reply_mode(thread_state,
                    source_message_id=thread_state.last_chain_message_id,
                    send=lambda target: self._safe_send_message(chat_id=thread_state.primary_chat_id,
                        text=f"Could not attach {receipt['name']}: {receipt.get('error', 'Upload could not be confirmed')}. Ask the agent to return it again after resolving this.",
                        reply_to_message_id=target))
                if sent:
                    self.state.bind_message(sent.chat_id, sent.message_id, thread_state.thread_id)
                    receipt["notice_sent"] = True
                    await self._save_state()
                else:
                    complete = False
        return complete

    async def _sync_turn_state(self, thread_state, conversation):
        if thread_state.thread_id in self._threads_with_send_in_flight:
            return False
        await self._dispatch_queued_input(thread_state)
        conversation = await self.desktop.read_thread(thread_state.thread_id)
        if conversation is None:
            return False
        # The app can consume several native queue entries between polls. Keep
        # each confirmed Telegram turn's result, even when it is no longer latest.
        for turn in conversation.turns:
            if turn.is_terminal and self._terminal_turn_needs_delivery(thread_state, turn):
                self._stage_terminal_output(thread_state, turn)
        # A failed outbound reply must not hold up new incoming instructions.
        await self._drain_outputs(thread_state)
        changed = await super()._sync_turn_state(thread_state, conversation)
        await self._dispatch_queued_input(thread_state)
        return changed

    async def _sync_approval_requests(self, thread_state, conversation):
        turn = conversation.latest_turn
        if not turn or not self._is_telegram_turn(thread_state, turn.turn_id):
            return False
        return await super()._sync_approval_requests(thread_state, conversation)

    async def _mark_pending_messages_done(self, thread_state):
        waiting = []
        for mid in thread_state.pending_message_ids:
            chat_id = thread_state.primary_chat_id
            receipt = thread_state.input_receipts.get(f'{chat_id}:{mid}', {})
            if (receipt.get('status') == 'confirmed'
                    and thread_state.output_deliveries.get(receipt.get('turn_id'), {}).get('status') == 'sent'):
                self._record_reaction(thread_state, chat_id, mid, 'done')
            else:
                waiting.append(mid)
        thread_state.pending_message_ids = waiting
        await self._save_state()
        await self._flush_reactions(thread_state)

    async def _create_approval_prompt(self, thread_state, request):
        # Do not offer an approval button that could target another visible request.
        key = (thread_state.thread_id, request.request_id)
        notified = getattr(self, "_local_approval_notices", set())
        if key in notified:
            return False
        sent = await self._send_thread_text_reply(
            thread_state,
            text=f"{self.routing.titles[thread_state.thread_id]} needs approval in the desktop app.\n" + self._format_desktop_request_prompt(request),
            reply_to_message_id=self._reply_target_for_thread(thread_state),
        )
        if sent:
            notified.add(key)
            self._local_approval_notices = notified
        return bool(sent)


def main():
    import os
    from . import cli, diagnostics
    os.umask(0o077)
    cli.BridgeApp = LocalBridgeApp
    diagnostics.CodexDesktopClient = SharedDesktopClient
    cli.main()


if __name__ == "__main__":
    main()
