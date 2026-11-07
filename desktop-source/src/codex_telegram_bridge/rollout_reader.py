"""Read bounded, incremental task events; never relay tools or internal messages."""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from uuid import UUID


class RolloutError(RuntimeError):
    pass


class RolloutReader:
    CHUNK = 1024 * 1024
    MAX_LINE = 256 * 1024
    INITIAL_TAIL = 8 * 1024 * 1024
    MAX_TAIL = 64 * 1024 * 1024
    KEEP_TURNS = 32
    DISCOVERY_INTERVAL = 5.0

    def __init__(self, thread_id: str, sessions: Path):
        UUID(thread_id)  # The filename pattern must never contain glob metacharacters.
        self.thread_id = thread_id
        self.sessions = sessions
        self.path, meta = self._find_transcript()
        self._next_discovery_at = time.monotonic() + self.DISCOVERY_INTERVAL
        self.cwd = meta.get("cwd")
        self.turns: OrderedDict[str, dict] = OrderedDict()
        self.offset = 0
        self.partial = b""
        self.skipping = False
        self.inode = self.path.stat().st_ino
        size = self.path.stat().st_size
        window = self.INITIAL_TAIL
        while True:
            self.turns.clear()
            self.offset = max(0, size - window)
            self.partial = b""
            self.skipping = self.offset > 0
            self._refresh_current()
            if self.turns or self.offset == 0 or window >= self.MAX_TAIL or window >= size:
                break
            window *= 2
        if not self.turns:
            raise RolloutError("No recent task lifecycle event found; refusing to guess whether it is idle")
        self.history_window = window
        self.history_start = max(0, size - window)

    def _find_transcript(self):
        # Resuming a task can leave the original file frozen and append to
        # <thread UUID>_<session UUID>.jsonl instead. Both retain the task's
        # identity in session_meta. Never infer identity from a substring or
        # choose by mtime: an older file may be touched after the continuation.
        paths = list(self.sessions.glob(f"*/*/*/rollout-*-{self.thread_id}.jsonl"))
        for path in self.sessions.glob(f"*/*/*/rollout-*-{self.thread_id}_*.jsonl"):
            suffix = path.stem.rsplit(f"{self.thread_id}_", 1)[-1]
            try:
                UUID(suffix)
            except ValueError:
                continue
            paths.append(path)
        candidates = []
        for path in paths:
            with path.open("rb") as handle:
                row = json.loads(handle.readline(self.MAX_LINE))
            meta = row.get("payload", {})
            if row.get("type") != "session_meta" or meta.get("id") != self.thread_id:
                raise RolloutError("Transcript identity does not match the registered task")
            candidates.append((path, meta))
        if not candidates:
            raise RolloutError("No local transcript for this registered task")
        if len(candidates) == 1:
            return candidates[0]
        dated = []
        for path, meta in candidates:
            try:
                stamp = datetime.fromisoformat(meta["timestamp"].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("Missing timezone")
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                raise RolloutError("Cannot order task transcript continuations safely") from exc
            dated.append((stamp, path, meta))
        dated.sort(key=lambda item: item[0])
        if dated[-1][0] == dated[-2][0]:
            raise RolloutError("Ambiguous task transcript continuations")
        return dated[-1][1], dated[-1][2]

    def expand_history(self):
        """Recover older pending receipts after restart, within the existing cap."""
        if self.history_start == 0 or self.history_window >= self.MAX_TAIL:
            return False
        stat = self.path.stat()
        if stat.st_ino != self.inode or stat.st_size < self.offset:
            raise RolloutError("Transcript was replaced or truncated; restart the bridge")
        self.history_window = min(self.history_window * 2, self.MAX_TAIL)
        self.history_start = max(0, stat.st_size - self.history_window)
        self.turns.clear()
        self.offset = self.history_start
        self.partial = b""
        self.skipping = self.offset > 0
        self.refresh()
        return True

    def refresh(self):
        if time.monotonic() >= self._next_discovery_at:
            path, _ = self._find_transcript()
            if path != self.path:
                # Build and validate the replacement before changing this
                # reader. A not-yet-started continuation must fail closed.
                newer = type(self)(self.thread_id, self.sessions)
                turns = OrderedDict(self.turns)
                for turn_id, turn in newer.turns.items():
                    previous = turns.pop(turn_id, None)
                    if previous:
                        seen = {item["id"] for item in previous["items"]}
                        turn = {**turn, "items": previous["items"] + [
                            item for item in turn["items"] if item["id"] not in seen]}
                    turns[turn_id] = turn
                while len(turns) > self.KEEP_TURNS:
                    turns.popitem(last=False)
                newer.turns = turns
                self.__dict__.update(newer.__dict__)
            self._next_discovery_at = time.monotonic() + self.DISCOVERY_INTERVAL
        return self._refresh_current()

    def _refresh_current(self):
        stat = self.path.stat()
        if stat.st_ino != self.inode or stat.st_size < self.offset:
            raise RolloutError("Transcript was replaced or truncated; restart the bridge")
        # Snapshot the size so a busy producer cannot keep this call running forever.
        end = stat.st_size
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            while self.offset < end:
                block = handle.read(min(self.CHUNK, end - self.offset))
                if not block:
                    break
                self.offset += len(block)
                self._feed(block)
        return list(self.turns.values())

    def _feed(self, block: bytes):
        parts = block.split(b"\n")
        for index, piece in enumerate(parts):
            complete = index < len(parts) - 1
            if not self.skipping:
                if len(self.partial) + len(piece) > self.MAX_LINE:
                    self.partial = b""
                    self.skipping = True
                else:
                    self.partial += piece
            if complete:
                if not self.skipping:
                    self._line(self.partial)
                self.partial = b""
                self.skipping = False

    def _line(self, line: bytes):
        # Large images, tool output and reasoning are not parsed or retained.
        if b'"event_msg"' not in line[:180]:
            return
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return
        if row.get("type") != "event_msg":
            return
        event = row.get("payload", {})
        kind = event.get("type")
        if kind not in {"task_started", "task_complete", "turn_aborted", "item_completed"}:
            return
        if event.get("thread_id", self.thread_id) != self.thread_id:
            return
        turn_id = event.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return
        item = event.get("item", {})
        if kind == "item_completed" and item.get("type") not in {"UserMessage", "AgentMessage"}:
            return
        if kind == "item_completed" and item.get("type") == "AgentMessage" and item.get("phase") != "final_answer":
            return
        turn = self.turns.setdefault(turn_id, {"turnId": turn_id, "status": "inProgress", "items": []})
        while len(self.turns) > self.KEEP_TURNS:
            self.turns.popitem(last=False)
        if kind == "task_complete":
            turn["status"] = "completed"
            final = event.get("last_agent_message")
            if isinstance(final, str) and final.strip() and not any(i["type"] == "agentMessage" for i in turn["items"]):
                turn["items"].append({"type": "agentMessage", "id": f"final:{turn_id}", "text": final})
        elif kind == "turn_aborted":
            turn["status"] = "interrupted"
        elif kind == "item_completed":
            content = item.get("content", [])
            # Retain only public text, never images, local file contents or tool items.
            text = "\n".join(p["text"] for p in content if isinstance(p, dict) and p.get("type") in {"text", "Text"} and isinstance(p.get("text"), str))
            if not text:
                return
            item_id = item.get("id") or f"{item['type']}:{turn_id}"
            if any(i["id"] == item_id for i in turn["items"]):
                return
            if item["type"] == "UserMessage":
                turn["items"].append({"type": "userMessage", "id": item_id, "content": [{"type": "text", "text": text}]})
            else:
                turn["items"].append({"type": "agentMessage", "id": item_id, "text": text})
