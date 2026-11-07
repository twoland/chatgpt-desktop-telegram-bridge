import json
import os
import time
from pathlib import Path

import pytest

from codex_telegram_bridge.rollout_reader import RolloutError, RolloutReader

TASK = "00000000-0000-4000-8000-000000000003"
SESSION = "00000000-0000-4000-8000-000000000006"
OTHER = "00000000-0000-4000-8000-000000000005"


def event(kind, turn, **fields):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn, **fields}}


def write_log(root, *, continuation=False, timestamp="2026-09-09T15:15:35Z",
              identity=TASK, rows=(), suffix=None):
    name = f"{TASK}_{SESSION}" if continuation else TASK
    if suffix is not None:
        name = f"{TASK}_{suffix}"
    folder = "2026/10/22" if continuation else "2026/09/09"
    path = root / folder / f"rollout-test-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"id": identity, "cwd": "/project"}
    if timestamp is not None:
        meta["timestamp"] = timestamp
    with path.open("w") as handle:
        for row in [{"type": "session_meta", "payload": meta}, *rows]:
            handle.write(json.dumps(row) + "\n")
    return path


def finished(turn, text):
    return [event("task_started", turn), event("task_complete", turn, last_agent_message=text)]


def test_frozen_original_does_not_hide_current_continuation(tmp_path):
    old = write_log(tmp_path, rows=finished("old", "obsolete"))
    current = write_log(tmp_path, continuation=True, timestamp="2026-10-22T14:22:00Z",
                        rows=finished("current", "Telegram reply"))
    # Fresh mtime is not evidence that a transcript is the current session.
    os.utime(old, (time.time() + 60, time.time() + 60))
    reader = RolloutReader(TASK, tmp_path)
    assert reader.path == current
    assert reader.refresh()[-1]["items"][0]["text"] == "Telegram reply"


def test_live_continuation_keeps_receipts_and_only_public_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(RolloutReader, "DISCOVERY_INTERVAL", 0)
    write_log(tmp_path, rows=[
        event("task_started", "started-before-resume"),
        event("item_completed", "started-before-resume", item={
            "type": "UserMessage", "id": "queued-request", "content": [
                {"type": "text", "text": "Telegram sender: owner\nMessage: test"}]}),
    ])
    reader = RolloutReader(TASK, tmp_path)
    current = write_log(tmp_path, continuation=True, timestamp="2026-10-22T14:22:00Z", rows=[
        event("item_completed", "started-before-resume", item={
            "type": "AgentMessage", "phase": "commentary", "content": [
                {"type": "Text", "text": "private progress"}]}),
        event("item_completed", "started-before-resume", item={
            "type": "CommandExecution", "text": "tool secret"}),
        event("task_complete", "started-before-resume", last_agent_message="public final"),
    ])
    turns = reader.refresh()
    assert reader.path == current
    assert turns[-1]["status"] == "completed"
    assert [item["type"] for item in turns[-1]["items"]] == ["userMessage", "agentMessage"]
    assert turns[-1]["items"][0]["id"] == "queued-request"
    assert "private progress" not in json.dumps(turns)
    assert "tool secret" not in json.dumps(turns)
    assert reader.refresh() == turns


def test_continuation_must_have_exact_registered_identity(tmp_path):
    write_log(tmp_path, rows=finished("old", "old"))
    write_log(tmp_path, continuation=True, timestamp="2026-10-22T14:22:00Z",
              identity=OTHER, rows=finished("foreign", "must not relay"))
    with pytest.raises(RolloutError, match="identity"):
        RolloutReader(TASK, tmp_path)


def test_non_session_suffix_is_not_a_continuation(tmp_path):
    original = write_log(tmp_path, rows=finished("registered", "safe"))
    write_log(tmp_path, continuation=True, suffix=SESSION + "-unrelated",
              timestamp="2026-10-22T14:22:00Z", rows=finished("foreign", "unsafe"))
    reader = RolloutReader(TASK, tmp_path)
    assert reader.path == original
    assert reader.refresh()[-1]["turnId"] == "registered"


@pytest.mark.parametrize("stamp", [None, "invalid", "2026-10-22T14:22:00"])
def test_ambiguous_undated_candidates_fail_closed(tmp_path, stamp):
    write_log(tmp_path, rows=finished("old", "old"))
    write_log(tmp_path, continuation=True, timestamp=stamp, rows=finished("new", "new"))
    with pytest.raises(RolloutError, match="order"):
        RolloutReader(TASK, tmp_path)


def test_equal_session_dates_fail_closed(tmp_path):
    write_log(tmp_path, rows=finished("old", "old"))
    write_log(tmp_path, continuation=True, rows=finished("new", "new"))
    with pytest.raises(RolloutError, match="Ambiguous"):
        RolloutReader(TASK, tmp_path)


def test_empty_continuation_never_replaces_a_valid_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(RolloutReader, "DISCOVERY_INTERVAL", 0)
    original = write_log(tmp_path, rows=finished("old", "old"))
    reader = RolloutReader(TASK, tmp_path)
    current = write_log(tmp_path, continuation=True, timestamp="2026-10-22T14:22:00Z")
    with pytest.raises(RolloutError, match="lifecycle"):
        reader.refresh()
    assert reader.path == original
    with current.open("a") as handle:
        handle.write(json.dumps(event("task_started", "new")) + "\n")
    assert reader.refresh()[-1]["turnId"] == "new"
    assert reader.path == current
