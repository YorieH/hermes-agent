"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_dispatcher_external_drain_pauses_then_resumes(monkeypatch, tmp_path):
    """A maintenance drain must quiesce ready work, then resume cleanly."""
    import asyncio
    from types import SimpleNamespace

    from gateway.run import GatewayRunner
    import hermes_cli.config as config_module
    import hermes_cli.kanban_db as kanban_db

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._external_drain_active = True

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
            }
        },
    )
    monkeypatch.setattr(kanban_db, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(kanban_db, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(
        kanban_db,
        "list_boards",
        lambda include_archived=False: [{"slug": kanban_db.DEFAULT_BOARD}],
    )
    monkeypatch.setattr(
        kanban_db,
        "kanban_db_path",
        lambda board=None: tmp_path / "kanban.db",
    )

    class FakeConnection:
        def close(self):
            return None

    monkeypatch.setattr(kanban_db, "connect", lambda board=None: FakeConnection())
    monkeypatch.setattr(kanban_db, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kanban_db, "has_spawnable_review", lambda conn: False)

    dispatch_calls = []

    def dispatch_once(conn, **kwargs):
        dispatch_calls.append(kwargs.get("board"))
        runner._running = False
        return SimpleNamespace(
            spawned=[], reclaimed=0, crashed=[], timed_out=[], promoted=0,
            auto_blocked=[],
        )

    monkeypatch.setattr(kanban_db, "dispatch_once", dispatch_once)

    sleeps = 0

    async def fake_sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        # The first dispatch interval is entirely drained.  Releasing the
        # marker allows exactly one dispatch on the following tick.
        if sleeps == 1:
            runner._external_drain_active = False

    monkeypatch.setattr("gateway.kanban_watchers.asyncio.sleep", fake_sleep)

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert dispatch_calls == [kanban_db.DEFAULT_BOARD]
    assert sleeps == 1


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)
