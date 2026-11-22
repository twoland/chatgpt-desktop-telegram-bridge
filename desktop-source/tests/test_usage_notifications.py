import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import codex_telegram_bridge.usage_notifications as module
from codex_telegram_bridge.bridge import BridgeApp
from codex_telegram_bridge.config import AppConfig, BridgeConfig, DesktopConfig, TelegramConfig, load_config
from codex_telegram_bridge.state import BridgeState
from codex_telegram_bridge.telegram_api import TelegramApiError
from codex_telegram_bridge.usage_notifications import UsageNotifications, read_usage, usage_command, usage_observations


def snapshot(percent=100, *, allowed=False, reached=None, secondary=None):
    bucket = {"limitId": "codex", "primary": {"usedPercent": percent, "resetsAt": 2000000000},
              "secondary": secondary, "rateLimitReachedType": reached}
    result = {"rateLimitsByLimitId": {"codex": bucket}}
    if allowed is not None:
        result["ordinaryUsageAllowed"] = allowed
    return result


@pytest.fixture
def monitor(tmp_path):
    config = AppConfig(TelegramConfig("fake", primary_chat_id=123), DesktopConfig(),
                       BridgeConfig(state_path=tmp_path / "state.json"))
    state = BridgeState()
    async def save():
        state.save(config.bridge.state_path)
    clock = [1000]
    m = UsageNotifications(config, state, send=AsyncMock(return_value=SimpleNamespace(message_id=77)),
                           save=save, read=AsyncMock(return_value=snapshot()), clock=lambda: clock[0])
    m.test_clock = clock
    return m


async def test_owner_only_warning_dedup_restart_and_verified_recovery(monitor):
    await monitor.tick()
    kwargs = monitor.send.call_args.kwargs
    assert kwargs["chat_id"] == 123 and set(kwargs) == {"chat_id", "text"}
    assert "usage limit" in kwargs["text"] and "Reported reset:" in kwargs["text"]
    await monitor.tick()
    loaded = BridgeState.load(monitor.config.bridge.state_path)
    restarted = UsageNotifications(monitor.config, loaded, send=monitor.send, save=AsyncMock(), read=monitor.read)
    await restarted.tick()
    monitor.send.assert_awaited_once()
    monitor.read.return_value = snapshot(5, allowed=True)
    await restarted.tick()
    assert monitor.send.await_count == 2
    assert "limit has cleared" in monitor.send.call_args.kwargs["text"]
    await restarted.tick()
    assert monitor.send.await_count == 2
    monitor.read.return_value = snapshot()
    await restarted.tick()
    assert monitor.send.await_count == 3  # A new episode, not suppressed forever.


@pytest.mark.parametrize("payload", [{}, None, {"rateLimits": None}, {"rateLimitsByLimitId": {}},
                                    {"rateLimitsByLimitId": {"codex": {"primary": None}}}])
async def test_missing_data_and_elapsed_reset_do_not_claim_recovery(monitor, payload):
    await monitor.tick()
    monitor.test_clock[0] = 2100000000
    monitor.read.return_value = payload
    await monitor.tick()
    assert monitor.data["active"] and monitor.send.await_count == 1


async def test_read_failure_does_not_claim_recovery_or_crash(monitor):
    await monitor.tick()
    monitor.read.side_effect = TimeoutError("unavailable")
    await monitor.tick()
    assert monitor.data["active"] and monitor.send.await_count == 1


async def test_partial_missing_secondary_does_not_clear_exhausted_window(monitor):
    monitor.read.return_value = snapshot(3, allowed=None, secondary={"usedPercent": 100})
    await monitor.tick()
    monitor.read.return_value = snapshot(4, allowed=None)
    await monitor.tick()
    assert monitor.data["active"] and monitor.send.await_count == 1
    monitor.read.return_value = snapshot(4, allowed=None, secondary={"usedPercent": 2})
    await monitor.tick()
    assert not monitor.data["active"] and monitor.send.await_count == 2


def test_available_with_credits_or_rounded_percent_is_not_a_block():
    observations = usage_observations(snapshot(100, allowed=True))
    assert not any(item["limited"] for item in observations.values())


@pytest.mark.parametrize("bucket", [{"rateLimitReachedType": "usageLimit"}, {"spendControlReached": True}])
async def test_explicit_server_limits_work_without_percent(monitor, bucket):
    monitor.read.return_value = {"rateLimitsByLimitId": {"codex": bucket}}
    await monitor.tick()
    monitor.send.assert_awaited_once()
    assert "Reported reset:" not in monitor.send.call_args.kwargs["text"]


async def test_model_specific_limit_and_multiple_buckets_share_one_episode(monitor):
    data = snapshot(4, allowed=True, reached="modelLimit")
    data["rateLimitsByLimitId"]["other"] = {"primary": {"usedPercent": 100}, "rateLimitReachedType": "rateLimit"}
    monitor.read.return_value = data
    await monitor.tick()
    monitor.read.return_value = snapshot(4, allowed=True)
    await monitor.tick()
    assert monitor.data["active"] and monitor.send.await_count == 1  # Missing other bucket is unknown.
    monitor.read.return_value["rateLimitsByLimitId"]["other"] = {"primary": {"usedPercent": 3}}
    await monitor.tick()
    assert not monitor.data["active"] and monitor.send.await_count == 2


def test_multi_bucket_view_precedes_legacy_and_bad_numbers_are_unknown():
    data = snapshot(5, allowed=None)
    data["rateLimits"] = {"primary": {"usedPercent": 100}}
    assert not any(x["limited"] for x in usage_observations(data).values())
    for value in (None, "100", True, float("nan"), float("inf"), -1):
        assert usage_observations(snapshot(value, allowed=None)) == {}


@pytest.mark.parametrize("owner", [None, -100123, 0])
async def test_never_infers_or_uses_group_destination(monitor, owner):
    monitor.config.telegram.primary_chat_id = owner
    monitor.state.primary_chat_id = 456  # The first chat is not an explicit owner grant.
    m = UsageNotifications(monitor.config, monitor.state, send=monitor.send, save=monitor.save, read=monitor.read)
    await m.tick()
    monitor.read.assert_not_awaited()
    monitor.send.assert_not_awaited()


async def test_disabled_never_polls(monitor):
    monitor.config.bridge.usage_notifications = False
    m = UsageNotifications(monitor.config, monitor.state, send=monitor.send, save=monitor.save, read=monitor.read)
    await m.run(asyncio.Event())
    monitor.read.assert_not_awaited()


async def test_definite_delivery_failure_backoff_and_retry_after(monitor):
    monitor.send.side_effect = TelegramApiError("429", retryable=True, retry_after=120)
    await monitor.tick()
    assert monitor.data["notice"]["status"] == "pending"
    assert monitor.data["notice"]["next_at"] == 1120
    await monitor.tick()
    assert monitor.send.await_count == 1
    monitor.test_clock[0] = 1120
    monitor.send.side_effect = TelegramApiError("connect", retryable=True)
    await monitor.tick()
    assert monitor.data["notice"]["next_at"] == 1124
    monitor.test_clock[0] = 1124
    monitor.send.side_effect = None
    await monitor.tick()
    assert monitor.data["notice"]["status"] == "sent"


@pytest.mark.parametrize("error", [TelegramApiError("timeout", uncertain=True), RuntimeError("unknown")])
async def test_uncertain_send_not_repeated_across_restart(monitor, error):
    monitor.send.side_effect = error
    await monitor.tick()
    loaded = BridgeState.load(monitor.config.bridge.state_path)
    restarted = UsageNotifications(monitor.config, loaded, send=monitor.send, save=AsyncMock(), read=monitor.read)
    await restarted.tick()
    assert loaded.usage_notifications["notice"]["status"] == "uncertain"
    monitor.send.assert_awaited_once()


async def test_sending_checkpoint_is_durable_before_network(monitor):
    async def check(**kwargs):
        persisted = BridgeState.load(monitor.config.bridge.state_path)
        assert persisted.usage_notifications["notice"]["status"] == "sending"
        raise asyncio.CancelledError()
    monitor.send.side_effect = check
    with pytest.raises(asyncio.CancelledError):
        await monitor.tick()
    restarted = UsageNotifications(monitor.config, BridgeState.load(monitor.config.bridge.state_path),
                                   send=monitor.send, save=AsyncMock(), read=monitor.read)
    await restarted.tick()
    monitor.send.assert_awaited_once()


async def test_no_recovery_message_for_warning_definitely_never_sent(monitor):
    monitor.send.side_effect = TelegramApiError("blocked", error_code=403)
    await monitor.tick()
    monitor.read.return_value = snapshot(4, allowed=True)
    await monitor.tick()
    assert "notice" not in monitor.data and monitor.send.await_count == 1


async def test_usage_reader_only_requests_limits_and_closes_on_cancellation(monitor, monkeypatch):
    calls = []
    class Client:
        def __init__(self, command, **kwargs):
            calls.append(("command", command))
        async def start(self): calls.append("initialize")
        async def request(self, method):
            calls.append(method)
            return snapshot()
        async def close(self): calls.append("close")
    monkeypatch.setattr(module, "CodexAppServerClient", Client)
    monitor.config.bridge.usage_command = ["fake-codex", "app-server"]
    assert await read_usage(monitor.config) == snapshot()
    assert calls == [("command", ["fake-codex", "app-server"]), "initialize", "account/rateLimits/read", "close"]
    async def cancelled(self, method): raise asyncio.CancelledError()
    monkeypatch.setattr(Client, "request", cancelled)
    with pytest.raises(asyncio.CancelledError): await read_usage(monitor.config)
    assert calls[-1] == "close"


def test_command_resolution_and_config_validation(monitor, tmp_path, monkeypatch):
    resources = tmp_path / "Example.app/Contents/Resources/codex-cli/bin"
    resources.mkdir(parents=True)
    (resources / "codex").touch()
    monitor.config.desktop.app_path = tmp_path / "Example.app"
    assert usage_command(monitor.config) == [str(resources / "codex"), "app-server"]
    monitor.config.desktop.app_path = tmp_path / "Missing.app"
    monkeypatch.setattr(module.shutil, "which", lambda name: "/bin/codex")
    assert usage_command(monitor.config) == ["/bin/codex", "app-server"]
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError): usage_command(monitor.config)
    for interval in (0, -1, 14, float("inf"), float("nan")):
        with pytest.raises(ValueError): BridgeConfig(usage_poll_interval_seconds=interval)
    for command in ("codex app-server", [], ["codex", ""], [None]):
        with pytest.raises(ValueError): BridgeConfig(usage_command=command)
    path = tmp_path / "config.toml"
    path.write_text('[telegram]\nbot_token="fake"\n[bridge]\nusage_notifications=false\nusage_poll_interval_seconds=120\nusage_command=["codex", "app-server"]\n')
    config = load_config(path)
    assert config.bridge.usage_notifications is False and config.bridge.usage_poll_interval_seconds == 120
    assert config.bridge.usage_command == ["codex", "app-server"]


def test_old_state_migration_and_round_trip(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 6, "threads": {}}))
    state = BridgeState.load(path)
    assert state.version == 7 and state.usage_notifications == {}
    state.usage_notifications = {"active": {"account": {"limited": True}}, "notice": {"status": "sent"}}
    state.save(path)
    assert BridgeState.load(path).usage_notifications == state.usage_notifications


async def test_notification_loop_independent_of_blocked_desktop_sync_and_cleans_up(monitor, monkeypatch):
    checked, syncing = asyncio.Event(), asyncio.Event()
    desktop = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page_title="Codex", page_url="app://-/", debugger_url="local")),
                              wait_until_task_index_ready=AsyncMock(), close=AsyncMock())
    app = BridgeApp(monitor.config, monitor.state, desktop=desktop)
    await app.telegram.close()
    app.telegram = SimpleNamespace(get_me=AsyncMock(return_value={"id": 42}), send_message=monitor.send, close=AsyncMock())
    app._cleanup_stale_approval_messages = AsyncMock()
    async def blocked():
        syncing.set()
        await asyncio.Event().wait()
    app._sync_all_threads_once = blocked
    real_init = UsageNotifications.__init__
    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        async def read(config):
            checked.set()
            return snapshot()
        self.read = read
    monkeypatch.setattr(UsageNotifications, "__init__", init)
    runner = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(syncing.wait(), 1)
        await asyncio.wait_for(checked.wait(), 1)
        # Let the in-flight notice finish before cancellation.
        for _ in range(10):
            if monitor.send.await_count: break
            await asyncio.sleep(0)
        monitor.send.assert_awaited_once()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert not app._tasks
    desktop.close.assert_awaited_once()


async def test_repairing_owner_never_replays_previous_owners_pending_notice(monitor):
    await monitor.observe(snapshot())
    monitor.config.telegram.primary_chat_id = 456
    new = UsageNotifications(monitor.config, monitor.state, send=monitor.send, save=monitor.save,
                             read=AsyncMock(return_value=snapshot(4, allowed=True)))
    await new.tick()
    monitor.send.assert_not_awaited()
    assert new.data["owner"] == 456 and not new.data["active"]


async def test_background_loop_survives_unexpected_failure(monitor):
    shutdown = asyncio.Event()
    calls = []
    async def tick():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("temporary state write failure")
        shutdown.set()
    monitor.tick = tick
    monitor.config.bridge.usage_poll_interval_seconds = 0.001
    await asyncio.wait_for(monitor.run(shutdown), 1)
    assert len(calls) == 2
