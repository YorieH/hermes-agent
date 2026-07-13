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
    adapter._polling_stale_generation_count = 2
    adapter._begin_polling_progress()
    assert adapter._polling_stale_generation_count == 2

    class _FakeHTTPXRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def do_request(self, *args, **kwargs):
            return 200, b'{"ok": true, "result": []}'

    monkeypatch.setattr(tg_adapter, "HTTPXRequest", _FakeHTTPXRequest)
    request = tg_adapter._build_polling_progress_request(
        adapter._record_polling_success,
        adapter._get_polling_generation,
    )

    # PTB's timeout=0 cleanup request is not steady-state progress.
    await request.do_request(
        request_data=SimpleNamespace(parameters={"timeout": 0})
    )
    assert adapter._polling_progress.last_success_at is None
    assert adapter._polling_stale_generation_count == 2

    clock.advance(25)
    await request.do_request(
        request_data=SimpleNamespace(parameters={"timeout": 10})
    )
    assert adapter._polling_progress.last_success_at == "2026-07-14T00:00:25+00:00"
    assert adapter._polling_stale_generation_count == 0
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


def test_disconnect_publishes_stopped_poll_health():
    """A drained poll completion cannot clear the breaker or stopped state."""
    clock = _FakeClock()
    adapter = _adapter(clock)
    adapter._begin_polling_progress()
    poll_generation = adapter._get_polling_generation()
    clock.advance(25)
    adapter._record_polling_success(poll_generation)
    adapter._polling_stale_generation_count = 2

    adapter._mark_disconnected()
    clock.advance(1)
    adapter._record_polling_success(poll_generation)

    # PTB can finish an in-flight long poll while disconnect() drains the
    # updater. That completion is teardown noise: it must not refresh progress,
    # reset the consecutive-generation breaker, or repaint status as healthy.
    assert adapter._polling_progress.last_success_at == "2026-07-14T00:00:25+00:00"
    assert adapter._polling_stale_generation_count == 2
    assert adapter._status_events[-1] == (
        "telegram-poll-stopped",
        {
            "poll_state": "stopped",
            "poll_last_success_at": "2026-07-14T00:00:25+00:00",
            "poll_stale_after_seconds": 180.0,
        },
    )

    # Only a success from a newly active generation clears the breaker.
    adapter._mark_connected()
    adapter._begin_polling_progress()
    active_generation = adapter._get_polling_generation()
    clock.advance(1)
    adapter._record_polling_success(active_generation)
    assert adapter._polling_stale_generation_count == 0
    assert adapter._polling_progress.last_success_at == "2026-07-14T00:00:27+00:00"


@pytest.mark.asyncio
async def test_old_inflight_poll_cannot_validate_replacement_generation(monkeypatch):
    """The request wrapper binds completion to its generation at request start."""
    clock = _FakeClock()
    adapter = _adapter(clock)
    adapter._polling_stale_generation_count = 2
    adapter._begin_polling_progress()
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    class _BlockingHTTPXRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def do_request(self, *args, **kwargs):
            request_started.set()
            await release_request.wait()
            return 200, b'{"ok": true, "result": []}'

    monkeypatch.setattr(tg_adapter, "HTTPXRequest", _BlockingHTTPXRequest)
    request = tg_adapter._build_polling_progress_request(
        adapter._record_polling_success,
        adapter._get_polling_generation,
    )
    old_poll = asyncio.create_task(
        request.do_request(
            request_data=SimpleNamespace(parameters={"timeout": 10})
        )
    )
    await request_started.wait()

    # Recovery begins a replacement generation while the old request is still
    # draining. Its later 2xx response cannot validate the replacement.
    adapter._begin_polling_progress()
    release_request.set()
    await old_poll
    assert adapter._polling_stale_generation_count == 2
    assert adapter._polling_progress.last_success_at is None
    assert adapter._status_events[-1][0] == "telegram-poll-waiting"

    clock.advance(1)
    await request.do_request(
        request_data=SimpleNamespace(parameters={"timeout": 10})
    )
    assert adapter._polling_stale_generation_count == 0
    assert adapter._polling_progress.last_success_at == "2026-07-14T00:00:01+00:00"


def test_webhook_mode_publishes_non_polling_health():
    """Webhook delivery is explicit instead of inheriting stale poll health."""
    clock = _FakeClock()
    adapter = _adapter(clock)
    adapter._polling_progress.last_success_at = "2026-07-13T23:59:00+00:00"

    adapter._mark_webhook_polling_state()

    assert adapter._status_events[-1] == (
        "telegram-webhook-mode",
        {
            "poll_state": "webhook",
            "poll_last_success_at": "2026-07-13T23:59:00+00:00",
            "poll_stale_after_seconds": 180.0,
        },
    )


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
    assert adapter._polling_stale_generation_count == 1
    assert adapter._status_events[-1][0] == "telegram-poll-stale"


@pytest.mark.asyncio
async def test_three_silent_generations_escalate_retryable_fatal_exactly_once():
    """Repeated successful starts without real polls force supervisor restart."""
    clock = _FakeClock()
    adapter = _adapter(clock)
    recovery_calls = 0
    fatal_notifications = 0

    async def _recover(error: Exception) -> None:
        nonlocal recovery_calls
        recovery_calls += 1

    async def _notify(adapter_arg: TelegramAdapter) -> None:
        nonlocal fatal_notifications
        assert adapter_arg is adapter
        fatal_notifications += 1

    adapter._handle_polling_network_error = _recover  # type: ignore[method-assign]
    adapter.set_fatal_error_handler(_notify)

    for generation in range(1, 4):
        adapter._begin_polling_progress()
        clock.advance(180)
        assert adapter._recover_stale_polling_if_needed() is True
        task = adapter._polling_error_task
        assert task is not None
        await task
        assert adapter._polling_stale_generation_count == generation

    assert recovery_calls == 2
    assert adapter.has_fatal_error is True
    assert adapter.fatal_error_code == "telegram_polling_stale"
    assert adapter.fatal_error_retryable is True
    assert fatal_notifications == 1

    # Fatal state is a hard one-shot guard even though the progress clock is
    # still stale; no fourth recovery or notification can be scheduled.
    assert adapter._recover_stale_polling_if_needed() is False
    await asyncio.sleep(0)
    assert recovery_calls == 2
    assert fatal_notifications == 1


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
    adapter._record_polling_success(adapter._get_polling_generation())
    clock.advance(179.9)
    assert adapter._recover_stale_polling_if_needed() is False
    assert adapter._polling_error_task is None


@pytest.mark.asyncio
async def test_stale_probe_never_starts_duplicate_concurrent_poller():
    """Repeated stale checks share one in-flight stop/drain/start owner."""
    clock = _FakeClock()
    adapter = _adapter(clock)
    adapter._begin_polling_progress()
    stale_generation = adapter._get_polling_generation()
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
    assert adapter._polling_stale_generation_count == 1
    assert adapter._get_polling_generation() is None

    # A 2xx from the poll task currently being drained is not recovery proof.
    clock.advance(1)
    adapter._record_polling_success(stale_generation)
    assert adapter._polling_stale_generation_count == 1
    assert adapter._status_events[-1][0] == "telegram-poll-stale"

    release.set()
    task = adapter._polling_error_task
    assert task is not None
    await task
    assert recovery_calls == 1
