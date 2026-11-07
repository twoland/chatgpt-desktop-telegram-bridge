"""Chat permissions enforced by the relay, independently of message text."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile

DEFAULT_AGENTS = {}  # Only explicitly registered local tasks may be routed.
RESERVED = {"start", "help", "ask", "attach", "detach", "agents", "agent", "status", "chatid", "replymode"}
REPLY_MODES = {"no", "first", "all"}


def with_reply_mode(raw, mode, chat_id=None):
    """Change only presentation; normal ChatRouting validation still applies."""
    if mode not in REPLY_MODES:
        raise ValueError("Reply mode must be no, first or all")
    updated = deepcopy(raw)
    if chat_id is None:
        updated["reply_mode"] = mode
    else:
        updated.setdefault("reply_modes", {})[str(chat_id)] = mode
    return updated


def save_routing(path, raw):
    with NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        json.dump(raw, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ChatRouting:
    def __init__(self, owner: int, raw: dict):
        if type(owner) is not int or owner <= 0:
            raise ValueError("Pair a private owner chat first")
        if raw.get("version") != 1:
            raise ValueError("Unsupported chat-routing version")
        self.owner = owner
        self.agents = raw.get("agents", {})
        self.groups = {}
        self.thread_chats = {}
        self.titles = {}
        self.file_roots = {}
        for alias, agent in self.agents.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", alias) or alias in RESERVED:
                raise ValueError("Invalid or reserved agent alias")
            thread_id = agent.get("thread_id")
            title = agent.get("title")
            if not isinstance(thread_id, str) or not thread_id.strip() or not isinstance(title, str) or not title.strip():
                raise ValueError("Each agent needs a task ID and title")
            if thread_id in self.titles:
                raise ValueError("Each alias must identify a distinct task")
            self.titles[thread_id] = title
            roots = agent.get("file_roots", [])
            if not isinstance(roots, list) or any(not isinstance(root, str) or not Path(root).is_absolute() or Path(root) == Path("/") for root in roots):
                raise ValueError("File roots must be absolute project-folder paths")
            self.file_roots[thread_id] = [Path(root) for root in roots]
        for key, group in raw.get("groups", {}).items():
            chat_id = int(key)
            if chat_id >= 0 or str(chat_id) != key:
                raise ValueError("Group IDs must be canonical negative Telegram chat IDs")
            alias = group.get("agent")
            if alias not in self.agents:
                raise ValueError("Group references an unknown agent")
            allowed = group.get("allowed_user_ids", [])
            if not isinstance(allowed, list) or any(type(user) is not int or user <= 0 for user in allowed):
                raise ValueError("Allowed users must be positive numeric Telegram user IDs")
            everyone = group.get("all_members", False)
            if type(everyone) is not bool:
                raise ValueError("all_members must be boolean")
            follow_replies = group.get("follow_reply_chains", False)
            if type(follow_replies) is not bool:
                raise ValueError("follow_reply_chains must be boolean")
            receive_all = group.get("receive_all_messages", False)
            if type(receive_all) is not bool:
                raise ValueError("receive_all_messages must be boolean")
            thread_id = self.agents[alias]["thread_id"]
            if thread_id in self.thread_chats:
                raise ValueError("A task cannot be assigned to more than one group")
            self.groups[chat_id] = {**group, "allowed_user_ids": allowed, "all_members": everyone,
                                    "follow_reply_chains": follow_replies, "receive_all_messages": receive_all}
            self.thread_chats[thread_id] = chat_id

        self.default_private_agent = raw.get("default_private_agent")
        if self.default_private_agent is not None:
            if (not isinstance(self.default_private_agent, str)
                    or self.default_private_agent not in self.agents):
                raise ValueError("default_private_agent must name a registered agent")
            if not self.allowed(owner, self.agents[self.default_private_agent]["thread_id"]):
                raise ValueError("default_private_agent must be available in the owner's private chat")

        self.reply_mode = raw.get("reply_mode", "all")
        if not isinstance(self.reply_mode, str) or self.reply_mode not in REPLY_MODES:
            raise ValueError("Reply mode must be no, first or all")
        overrides = raw.get("reply_modes", {})
        if not isinstance(overrides, dict):
            raise ValueError("reply_modes must map configured chat IDs to modes")
        self.reply_modes = {}
        for key, mode in overrides.items():
            if (not isinstance(key, str) or not re.fullmatch(r"-?[1-9][0-9]*", key)
                    or int(key) not in {owner, *self.groups}):
                raise ValueError("Reply mode override needs the owner chat or a configured group")
            if not isinstance(mode, str) or mode not in REPLY_MODES:
                raise ValueError("Reply mode must be no, first or all")
            self.reply_modes[int(key)] = mode

    def reply_mode_for(self, chat_id):
        return self.reply_modes.get(chat_id, self.reply_mode)

    @classmethod
    def load(cls, owner: int, path: Path):
        raw = json.loads(path.read_text()) if path.exists() else {"version": 1, "agents": DEFAULT_AGENTS, "groups": {}}
        return cls(owner, raw)

    def accepts_sender(self, chat: dict, sender: dict, *, anonymous=False):
        chat_id, user = chat.get("id"), sender.get("id")
        if anonymous or sender.get("is_bot") or type(user) is not int or user <= 0:
            return False
        if chat.get("type") == "private":
            return chat_id == self.owner == user
        group = self.groups.get(chat_id)
        return bool(chat.get("type") in {"group", "supergroup"} and group and
                    (user == self.owner or group["all_members"] or user in group["allowed_user_ids"]))

    def allowed(self, chat_id, thread_id):
        if thread_id not in self.titles:
            return False
        destination = self.thread_chats.get(thread_id, self.owner)
        return chat_id == destination

    def agents_for(self, chat_id):
        return {alias: agent for alias, agent in self.agents.items() if self.allowed(chat_id, agent["thread_id"])}

    def resolve(self, chat_id, value):
        agents = self.agents_for(chat_id)
        if value in agents:
            return agents[value]["thread_id"]
        return value if self.allowed(chat_id, value) else None

    def default_private_thread(self, chat_id):
        if chat_id != self.owner or self.default_private_agent is None:
            return None
        return self.resolve(chat_id, self.default_private_agent)

    def group_thread(self, chat_id):
        group = self.groups.get(chat_id)
        return self.agents[group["agent"]]["thread_id"] if group else None

    def validate_state(self, state):
        for thread in state.threads.values():
            if not self.allowed(thread.primary_chat_id, thread.thread_id):
                raise ValueError("Saved chat binding conflicts with routing. Detach that task before reassigning it.")
            if any(not self.allowed(job.chat_id, thread.thread_id) for job in thread.queued_inputs):
                raise ValueError("Saved queue contains input from a different chat")
        for key, thread_id in state.message_bindings.items():
            chat_id = int(key.split(":", 1)[0])
            if not self.allowed(chat_id, thread_id):
                raise ValueError("Saved message binding conflicts with chat routing")
