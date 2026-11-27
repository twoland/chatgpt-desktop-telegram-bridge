import json

import codex_telegram_bridge.local_mode as local_mode
from codex_telegram_bridge.config import AppConfig, BridgeConfig, DesktopConfig, TelegramConfig


def test_local_doctor_uses_registered_routes_without_widening_access(tmp_path):
    config = AppConfig(
        telegram=TelegramConfig(bot_token="test-token", primary_chat_id=1234, allowed_chat_ids=[1234]),
        desktop=DesktopConfig(),
        bridge=BridgeConfig(state_path=tmp_path / "state.json"),
    )
    (tmp_path / "chat-routing.json").write_text(json.dumps({
        "version": 1,
        "agents": {"assistant": {"thread_id": "registered-task", "title": "Assistant"}},
        "groups": {},
        "default_private_agent": "assistant",
    }))

    client = local_mode.build_local_diagnostic_client(config)

    assert client.allowed_thread_ids == {"registered-task"}
    assert client.task_titles == {"registered-task": "Assistant"}
    client.check_thread("registered-task")

    import pytest
    from codex_telegram_bridge.desktop_client import DesktopClientError
    with pytest.raises(DesktopClientError, match="not enabled"):
        client.check_thread("unregistered-task")


def test_local_entrypoint_installs_route_aware_diagnostics(monkeypatch):
    from codex_telegram_bridge import cli, diagnostics

    monkeypatch.setattr(cli, "BridgeApp", cli.BridgeApp)
    monkeypatch.setattr(diagnostics, "build_desktop_client", diagnostics.build_desktop_client)
    observed = []
    monkeypatch.setattr(cli, "main", lambda: observed.append(diagnostics.build_desktop_client))
    local_mode.main()

    assert observed == [local_mode.build_local_diagnostic_client]
