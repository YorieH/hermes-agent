"""Deterministic coverage for Telegram's successful-poll heartbeat.

The pre-existing getMe/pending_update_count probes cannot prove that an idle
getUpdates request is completing when Telegram has no queued messages. These
tests use a fake monotonic/wall clock to cover that otherwise silent state
without sleeping or contacting Telegram.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.epoch = datetime(2026, 7, 14, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.value

    def wall_clock(self) -> datetime:
        return self.epoch + timedelta(seconds=self.value)

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _adapter(clock: _FakeClock, *, stale_after: float = 180.0) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._polling_progress = tg_adapter._PollingProgress(
        stale_after,
        monotonic=clock.monotonic,
        wall_clock=clock.wall_clock,
    )
    adapter._polling_progress_last_status_write = None
    adapter._webhook_mode = False
    adapter._app = SimpleNamespace(updater=SimpleNamespace(running=True))
    adapter._polling_error_task = None
    adapter._background_tasks = set()
    adapter._status_events = []
    adapter._write_runtime_status_safe = (  # type: ignore[method-assign]
        lambda context, **fields: adapter._status_events.append((context, fields))
    )
    return adapter


@pytest.mark.asyncio
async def test_healthy_idle_polls_advance_sanitized_progress(monkeypatch):
    """An empty successful long poll advances health; shutdown ACKs do not."""
    clock = _FakeClock()
    adapter = _adapter(clock)
    adapter._begin_polling_progress()

    class _FakeHTTPXRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def do_request(self, *args, **kwargs):
            return 200, b'{"ok": true, "result": []}'

    monkeypatch.setattr(tg_adapter, "HTTPXRequest", _FakeHTTPXRequest)
    request = tg_adapter._build_polling_progress_request(
        adapter._record_polling_success
    )

    # PTB's timeout=0 cleanup request is not steady-state progress.
    await request.do_request(
        request_data=SimpleNamespace(parameters={"timeout": 0})
    )
    assert adapter._polling_progress.last_success_at is None

    clock.advance(25)
    await request.do_request(
        request_data=SimpleNamespace(parameters={"timeout": 10})
    )
    assert adapter._polling_progress.last_success_at == "2026-07-14T00:00:25+00:00"
    assert adapter._status_events[-1] == (
        "telegram-poll-progress",
        {
            "poll_state": "healthy",
            "poll_last_success_at": "2026-07-14T00:00:25+00:00",
            "poll_stale_after_seconds": 180.0,
        },
    )

    clock.advance(120)
    assert adapter._recover_stale_polling_if_needed() is False


@pytest.mark.asyncio
async def test_one_hung_stale_poll_schedules_existing_recovery_once():
    """One poller beyond the conservative deadline enters the existing ladder."""
    clock = _FakeClock()
    adapter = _adapter(clock)
    adapter._begin_polling_progress()
    clock.advance(180)

    recovered = []

    async def _recover(error: Exception) -> None:
        recovered.append(type(error).__name__)

    adapter._handle_polling_network_error = _recover  # type: ignore[method-assign]
    assert adapter._recover_stale_polling_if_needed() is True
    task = adapter._polling_error_task
    assert task is not None
    await task

    assert recovered == ["TimeoutError"]
    assert adapter._status_events[-1][0] == "telegram-poll-stale"


@pytest.mark.asyncio
async def test_transient_slow_poll_below_budget_does_not_reconnect():
    """A slow request below the four-budget deadline remains healthy."""
    assert tg_adapter._poll_progress_stale_after(20.0) == 180.0
    assert tg_adapter._poll_progress_stale_after(100.0) == 440.0
    clock = _FakeClock()
    adapter = _adapter(clock)
    adapter._begin_polling_progress()

    clock.advance(179.9)
    assert adapter._recover_stale_polling_if_needed() is False

    # A late but successful request resets the monotonic deadline.
    adapter._record_polling_success()
    clock.advance(179.9)
    assert adapter._recover_stale_polling_if_needed() is False
    assert adapter._polling_error_task is None


@pytest.mark.asyncio
async def test_stale_probe_never_starts_duplicate_concurrent_poller():
    """Repeated stale checks share one in-flight stop/drain/start owner."""
    clock = _FakeClock()
    adapter = _adapter(clock)
    adapter._begin_polling_progress()
    clock.advance(180)

    entered = asyncio.Event()
    release = asyncio.Event()
    recovery_calls = 0

    async def _recover(error: Exception) -> None:
        nonlocal recovery_calls
        recovery_calls += 1
        entered.set()
        await release.wait()

    adapter._handle_polling_network_error = _recover  # type: ignore[method-assign]
    assert adapter._recover_stale_polling_if_needed() is True
    await entered.wait()

    # A second heartbeat while recovery owns stop/drain/start is a no-op.
    assert adapter._recover_stale_polling_if_needed() is False
    assert recovery_calls == 1

    release.set()
    task = adapter._polling_error_task
    assert task is not None
    await task
    assert recovery_calls == 1
