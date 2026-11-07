"""Current desktop UI input, with read-only local transcript reply tracking."""
from __future__ import annotations

import asyncio
import json
import re
import string
import html
from pathlib import Path

from .desktop_client import (
    CodexDesktopClient, DesktopClientError, DesktopConversationSummary,
    DesktopDraftConflictError, DesktopSendUnconfirmedError,
    DesktopQueuedReceipt, DesktopQueueModeError, DesktopTurn,
    _raise_if_thread_turn_is_active,
)
from .rollout_reader import RolloutError, RolloutReader


def normalized_editor_text(value: str) -> str:
    # Chromium innerText adds layout-dependent blank lines between paragraphs;
    # the desktop serializer removes empty ones. Keep nonempty lines unchanged.
    return "\n".join(line for line in value.replace("\r\n", "\n").strip().split("\n") if line.strip())


def matches_submitted_text(turn, expected):
    # Compare the original plain text against the desktop's Markdown serializer:
    # empty paragraphs disappear, punctuation is escaped and URLs become links.
    def decoded(value):
        def unescape_punctuation(text):
            return re.sub(r"\\([" + re.escape(string.punctuation) + r"])", r"\1", text)
        def unwrap_auto_link(match):
            label = unescape_punctuation(match[1])
            target = unescape_punctuation(match[2])
            # The editor escapes query separators in targets too. Compare both
            # decoded sides, but return the raw label so it is decoded only once.
            return match[1] if label == target and target.startswith(("https://", "http://", "file://")) else match[0]
        value = re.sub(r"\[((?:\\.|[^\]])+)\]\(((?:\\.|[^)\\\n])+)\)", unwrap_auto_link, value)
        value = unescape_punctuation(value)
        return normalized_editor_text(html.unescape(value))
    wanted = normalized_editor_text(expected)
    if not wanted:
        return False
    for item in turn.items:
        if item.get("type") != "userMessage":
            continue
        parts = [p["text"] for p in item.get("content", []) if isinstance(p, dict) and isinstance(p.get("text"), str)]
        saved = "\n".join(parts)
        candidates = [saved]
        if expected.startswith('Telegram sender: '):
            # The app may prepend ambient browser state to a submitted message.
            # Strip only that exact leading envelope, never arbitrary prefixes or
            # quoted messages. The complete request still has to match afterward.
            envelope = re.match(
                r'\A\s*<in-app-browser-context source="ambient-ui-state">.*?'
                r'</in-app-browser-context>\s*\n## My request:\s*\n(.*)\Z', saved, re.S)
            if envelope:
                candidates.append(envelope[1])
        if any(wanted in {normalized_editor_text(value), decoded(value)} for value in candidates):
            return True
    return False


INDEX_JS = r"""(() => {
  const rows = [...document.querySelectorAll('[data-app-action-sidebar-thread-row]')].map(n => ({
    id: n.getAttribute('data-app-action-sidebar-thread-id'),
    title: n.getAttribute('data-app-action-sidebar-thread-title')
  }));
  const root = document.getElementById('root');
  const key = root && Object.keys(root).find(k => k.startsWith('__reactContainer'));
  const container = key && root[key];
  const stack = container ? [container.stateNode?.current || container] : [];
  const seen = new Set(), active = new Set();
  while (stack.length) {
    const f = stack.pop();
    if (!f || seen.has(f)) continue;
    seen.add(f);
    if (typeof f.memoizedProps?.activeThreadId === 'string') active.add(f.memoizedProps.activeThreadId);
    if (f.child) stack.push(f.child);
    if (f.sibling) stack.push(f.sibling);
  }
  return {rows, active: [...active]};
})()"""


def composer_js(body: str, thread_id: str | None = None) -> str:
    guard = ""
    if thread_id is not None:
        guard = f"const index = {INDEX_JS}; if (index.active.length !== 1 || index.active[0] !== {json.dumps(thread_id)}) return {{ok:false,error:'wrong-task'}};"
    return """(() => {""" + guard + """
      const entries = [...document.querySelectorAll('[data-codex-composer][contenteditable="true"]')].filter(n => {
        const r=n.getBoundingClientRect(), s=getComputedStyle(n);
        return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none';
      });
      if (entries.length !== 1) return {ok:false,error:'ambiguous-or-missing-composer'};
      const composer=entries[0], panel=composer.closest('[data-composer-body]');
      if (!panel) return {ok:false,error:'missing-composer-panel'};
      // Inline suggestions are ProseMirror decorations, not document content.
      // innerText includes those suggestions and the "Press Tab to accept" hint.
      const editorText=()=>{
        const doc=composer.pmViewDesc?.node;
        if(doc?.type?.name!=='doc' || typeof doc.textBetween!=='function') return null;
        return doc.textBetween(0,doc.content.size,'\\n',node=>
          ['hardBreak','hard_break'].includes(node.type.name)?'\\n':'\\ufffc');
      };
    """ + body + "\n})()"


class CurrentDesktopClient(CodexDesktopClient):
    supports_native_queue = True

    def __init__(self, *, task_titles=None, sessions_path=None, **kwargs):
        super().__init__(**kwargs)
        self.task_titles = dict(task_titles or {})
        self.sessions_path = sessions_path or Path.home() / ".codex/sessions"
        self._readers = {}

    async def wait_until_task_index_ready(self):
        index = await self._eval_json(INDEX_JS)
        if not isinstance(index, dict) or not index.get("rows"):
            raise DesktopClientError("The main app's task sidebar is unavailable")

    async def list_threads(self):
        index = await self._eval_json(INDEX_JS) or {}
        active = index.get("active", [])
        result = []
        for thread_id, title in self.task_titles.items():
            result.append(DesktopConversationSummary(thread_id=thread_id, title=title, current=active == [thread_id], cwd=None))
        return result

    async def read_thread(self, thread_id):
        if thread_id not in self.task_titles:
            raise DesktopClientError("This task is not registered")
        try:
            if thread_id not in self._readers:
                self._readers[thread_id] = RolloutReader(thread_id, self.sessions_path)
            reader = self._readers[thread_id]
            turns = reader.refresh()
        except (RolloutError, OSError, ValueError) as exc:
            raise DesktopClientError(f"Cannot read the registered task transcript: {exc}") from exc
        return self._parse_conversation({"id": thread_id, "title": self.task_titles[thread_id], "cwd": reader.cwd,
            "hostId": "local", "source": "desktop", "turns": turns, "requests": []})

    async def _current_thread_id(self):
        index = await self._eval_json(INDEX_JS) or {}
        active = index.get("active", [])
        return active[0] if len(active) == 1 else None

    async def recover_submitted_inputs(self, thread_id, texts):
        """Expand bounded history for saved inputs whose turn predates the tail."""
        conversation = await self.read_thread(thread_id)
        reader = self._readers[thread_id]
        while any(not any(matches_submitted_text(turn, text) for turn in conversation.turns) for text in texts):
            try:
                expanded = reader.expand_history()
            except (RolloutError, OSError, ValueError) as exc:
                raise DesktopClientError(f"Cannot recover registered task receipts: {exc}") from exc
            if not expanded:
                break
            conversation = await self.read_thread(thread_id)
        return conversation

    async def read_composer_state(self):
        return await self._eval_json(composer_js("const text=editorText(); return text===null?{ok:false,error:'missing-editor-document'}:{ok:true,text};")) or {"ok": False, "text": ""}

    async def activate_thread(self, thread_id):
        if thread_id not in self.task_titles:
            raise DesktopClientError("This task is not registered")
        if await self._current_thread_id() != thread_id:
            # Do not switch away from a user's unsent draft.
            draft = await self.read_composer_state()
            if not draft.get("ok"):
                raise DesktopClientError("Cannot verify the current composer before switching tasks")
            if str(draft.get("text", "")).strip():
                raise DesktopDraftConflictError(context="current task", draft_text=draft["text"])
            title = self.task_titles[thread_id]
            clicked = await self._eval_json("""(() => {
              const rows=[...document.querySelectorAll('[data-app-action-sidebar-thread-row]')];
              let matches=rows.filter(n=>n.getAttribute('data-app-action-sidebar-thread-id')===""" + json.dumps("local:" + thread_id) + """);
              if (!matches.length) matches=rows.filter(n=>n.getAttribute('data-app-action-sidebar-thread-title')===""" + json.dumps(title) + """ && n.getAttribute('data-app-action-sidebar-thread-id')?.startsWith('local:client-new-thread:'));
              if (matches.length!==1) return {ok:false};
              matches[0].click(); return {ok:true};
            })()""")
            if not clicked or not clicked.get("ok"):
                raise DesktopClientError("Registered task is not uniquely visible in the sidebar")
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self._current_thread_id() == thread_id:
                return await self.read_thread(thread_id)
            await asyncio.sleep(.2)
        raise DesktopClientError("The selected task ID could not be verified; no message was entered")

    async def read_native_queue(self, thread_id):
        await self.activate_thread(thread_id)
        return await self._visible_native_queue(thread_id)

    async def _visible_native_queue(self, thread_id):
        # Read only the rendered queue's props. Never invoke React callbacks or
        # write application state; submission still uses the visible UI button.
        result = await self._eval_json(composer_js(r"""
          const root=document.getElementById('root');
          const key=Object.keys(root).find(k=>k.startsWith('__reactContainer'));
          const container=key && root[key];
          const stack=container ? [container.stateNode?.current || container] : [];
          const seen=new Set(), lists=[];
          while(stack.length) {
            const f=stack.pop(); if (!f || seen.has(f)) continue; seen.add(f);
            const p=f.memoizedProps;
            if (Array.isArray(p?.messages) && typeof p.onSendNowMessage==='function'
                && typeof p.onReorderMessages==='function') {
              lists.push(p.messages.map(m=>({id:m.id,text:m.text,pausedReason:m.pausedReason ?? null})));
            }
            if(f.child) stack.push(f.child); if(f.sibling) stack.push(f.sibling);
          }
          if(lists.length>1) return {ok:false,error:'ambiguous-native-queue'};
          if(!lists.length && document.querySelector('button[aria-label="Queued message actions"]'))
            return {ok:false,error:'unreadable-native-queue'};
          return {ok:true,messages:lists[0] || []};
        """, thread_id))
        if not isinstance(result, dict) or not result.get('ok'):
            raise DesktopClientError('Cannot verify the native message queue')
        messages = result.get('messages')
        if not isinstance(messages, list) or any(not isinstance(m, dict)
                or not isinstance(m.get('id'), str) or not m['id']
                or not isinstance(m.get('text'), str) for m in messages):
            raise DesktopClientError('Unrecognized native message queue')
        return messages

    @staticmethod
    def queued_receipt(messages, text):
        matches = [m for m in messages if matches_submitted_text(DesktopTurn(
            turn_id=m['id'], status='queued', items=[{'type':'userMessage',
            'content':[{'type':'text','text':m['text']}]}]), text)]
        if len(matches) > 1:
            raise DesktopClientError('Multiple native queue entries match this input; review the task')
        return DesktopQueuedReceipt(matches[0]['id']) if matches else None

    async def send_message(self, thread_id, text, *, recover_draft=False, checkpoint=None, queue_if_busy=False):
        before = await self.read_thread(thread_id)
        if not queue_if_busy:
            _raise_if_thread_turn_is_active(thread_id, before)
        await self.activate_thread(thread_id)
        baseline = await self.read_thread(thread_id)
        if not queue_if_busy:
            _raise_if_thread_turn_is_active(thread_id, baseline)
        else:
            receipt = self.queued_receipt(await self._visible_native_queue(thread_id), text)
            if receipt:
                return receipt
        previous = {t.turn_id for t in baseline.turns}
        state = await self.read_composer_state()
        if not state.get("ok"):
            raise DesktopClientError("Composer is unavailable")
        if str(state.get("text", "")).strip():
            if recover_draft and normalized_editor_text(state['text']) == normalized_editor_text(text):
                return await self._submit_and_wait(thread_id, text, previous, checkpoint=checkpoint, queue_if_busy=queue_if_busy)
            raise DesktopDraftConflictError(context="target task", draft_text=state["text"])
        focused = await self._eval_json(composer_js("""
          composer.focus(); const selection=window.getSelection(), range=document.createRange();
          range.selectNodeContents(composer); range.collapse(false); selection.removeAllRanges(); selection.addRange(range);
          return {ok:document.activeElement===composer};
        """, thread_id))
        if not focused or not focused.get("ok"):
            raise DesktopClientError("Cannot focus the verified target task")
        await self._call_cdp("Input.insertText", {"text": text}, _allow_reconnect=False)
        return await self._submit_and_wait(thread_id, text, previous, checkpoint=checkpoint, queue_if_busy=queue_if_busy)

    async def send_existing_draft(self, thread_id, text):
        """Explicit recovery only: submit the exact known draft, without inserting again."""
        await self.activate_thread(thread_id)
        baseline = await self.read_thread(thread_id)
        _raise_if_thread_turn_is_active(thread_id, baseline)
        state = await self.read_composer_state()
        if not state.get("ok") or normalized_editor_text(str(state.get("text", ""))) != normalized_editor_text(text):
            raise DesktopClientError("Recovery draft differs; nothing was sent")
        return await self._submit_and_wait(thread_id, text, {t.turn_id for t in baseline.turns})

    async def _submit_and_wait(self, thread_id, text, previous, *, checkpoint=None, queue_if_busy=False):
        # ProseMirror text updates before React replaces the voice button with Send.
        # Wait for both to agree, checking identity and every nonempty text line.
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        last_error = "composer-updating"
        while asyncio.get_running_loop().time() < deadline:
            if not queue_if_busy:
                _raise_if_thread_turn_is_active(thread_id, await self.read_thread(thread_id))
            if checkpoint:
                await checkpoint('verify')  # Persist before a click could happen.
            clicked = await self._eval_json(composer_js(r"""
          const text=editorText();
          if(text===null) return {ok:false,error:'missing-editor-document'};
          const normalizedText=text.replace(/\r\n/g,'\n').trim().split('\n').filter(line=>line.trim()).join('\n');
          if (normalizedText !== """ + json.dumps(normalized_editor_text(text)) + """ )
            return {ok:false,error:'composer-updating'};
          const allowed=""" + json.dumps(['Send message', 'Send', 'Queue'] if queue_if_busy else ['Send message', 'Send']) + """;
          const buttons=[...panel.querySelectorAll('button')];
          const matches=buttons.filter(b=>allowed.includes(b.getAttribute('aria-label')));
          if(!matches.length && buttons.some(b=>b.getAttribute('aria-label')==='Steer' && !b.disabled))
            return {ok:false,error:'choose-queue-follow-up-behavior-in-app-settings'};
          if (matches.length!==1 || matches[0].disabled) return {ok:false,error:'send-not-ready'};
          const mode=matches[0].getAttribute('aria-label');
          matches[0].click(); return {ok:true,mode};
        """, thread_id))
            if clicked and clicked.get("ok"):
                break  # Never click again after an acknowledged click.
            if checkpoint:
                await checkpoint('ready')  # The UI confirms no click occurred.
            last_error = (clicked or {}).get("error", "composer-unavailable")
            if last_error == 'choose-queue-follow-up-behavior-in-app-settings':
                raise DesktopQueueModeError('Set desktop Settings > General > Follow-up behavior to Queue; the draft was not submitted')
            if last_error not in {"composer-updating", "send-not-ready"}:
                raise DesktopClientError(f"Draft was not sent: {last_error}")
            await asyncio.sleep(.1)
        else:
            raise DesktopClientError(f"Draft was not sent: {last_error}; it remains in the desktop task")

        deadline = asyncio.get_running_loop().time() + self._send_ack_timeout_limit_seconds()
        while asyncio.get_running_loop().time() < deadline:
            current = await self.read_thread(thread_id)
            if any(t.turn_id not in previous and matches_submitted_text(t, text) for t in current.turns):
                return current
            if queue_if_busy:
                receipt = self.queued_receipt(await self._visible_native_queue(thread_id), text)
                if receipt:
                    return receipt
            await asyncio.sleep(.2)
        raise DesktopSendUnconfirmedError(thread_id=thread_id, expected_text=text, after_turn_count=len(previous))

    async def _eval_json(self, expression):
        # Replaying an uncertain UI click could submit twice. Reconnect on the next call.
        result = await self._call_cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, _allow_reconnect=False)
        if result.get("exceptionDetails"):
            raise DesktopClientError("Local UI evaluation failed")
        return (result.get("result") or {}).get("value")
