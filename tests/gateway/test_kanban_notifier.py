import asyncio
from pathlib import Path
from types import SimpleNamespace


from gateway.config import Platform
from gateway.session import SessionSource
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class RecordingHandleAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.events = []

    async def handle_message(self, event):
        self.events.append(event)


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_profile_owned_subscription_is_claimed_only_by_its_gateway(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "profile-owned.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned notification", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-asuna",
            notifier_profile="asuna",
        )
        kb.complete_task(conn, tid, summary="done by Asuna")
    finally:
        conn.close()

    kurumi_adapter = RecordingAdapter()
    kurumi_runner = _make_runner(kurumi_adapter)
    kurumi_runner._kanban_notifier_profile = "kurumi"
    asyncio.run(_run_one_notifier_tick(monkeypatch, kurumi_runner))

    assert kurumi_adapter.sent == []
    assert [
        ev.kind for ev in _unseen_terminal_events_for(tid, "chat-asuna")
    ] == ["completed"]

    asuna_adapter = RecordingAdapter()
    asuna_runner = _make_runner(asuna_adapter)
    asuna_runner._kanban_notifier_profile = "asuna"
    asyncio.run(_run_one_notifier_tick(monkeypatch, asuna_runner))

    assert len(asuna_adapter.sent) == 1
    assert asuna_adapter.sent[0]["chat_id"] == "chat-asuna"


def test_kanban_notifier_skips_external_wake_without_handle_message(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "send-only-adapter.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="send-only wake",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    with caplog.at_level("WARNING"):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "wakeup injection failed" not in caplog.text
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


def test_kanban_notifier_batches_same_session_agent_wakes(tmp_path, monkeypatch):
    db_path = tmp_path / "batched-agent-wakes.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_ids = []
        for index in range(2):
            tid = kb.create_task(
                conn,
                title=f"batched completion {index}",
                assignee="worker",
                session_id="agent:main:telegram:group:chat-1",
            )
            kb.add_notify_sub(
                conn,
                task_id=tid,
                platform="telegram",
                chat_id="chat-1",
            )
            kb.complete_task(conn, tid, summary=f"done {index}")
            task_ids.append(tid)
    finally:
        conn.close()

    adapter = RecordingHandleAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2
    assert len(adapter.events) == 1
    wake = adapter.events[0]
    assert wake.internal is True
    assert "2 task updates arrived together" in wake.text
    assert all(tid in wake.text for tid in task_ids)


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_owning_profile_adapter_no_default_fallback(tmp_path, monkeypatch):
    """A subscription owned by a secondary profile whose profile-adapter
    registry entry EXISTS but lacks this platform must NOT fall back to the
    default profile's same-platform adapter — the notifier must route through
    the shared ``_authorization_adapter`` chokepoint, which forbids that
    fallback (gateway/authz_mixin.py). Delivering via the default profile's bot
    is the exact cross-profile mis-delivery this whole change exists to fix
    (`[230002] Bot can NOT be out of the chat`).

    Mutation check: reverting kanban_watchers.py's adapter selection to the old
    inline ``if adapter is None: adapter = self.adapters.get(plat)`` fallback
    makes this test FAIL (the default adapter receives the delivery).
    """
    db_path = tmp_path / "profile-no-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        # Subscription is owned by profile "beta".
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    default_adapter = RecordingAdapter()
    other_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    # Default profile has a telegram adapter …
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    # … and profile "beta" HAS a non-empty registry entry (so it passes the
    # notifier's upstream skip-filter, which only skips owning profiles with NO
    # adapter at all), but that entry does NOT contain a telegram adapter — beta
    # connected a different platform (discord). The telegram sub owned by beta
    # must therefore resolve to NO adapter, not silently borrow the default
    # profile's telegram bot.
    runner._profile_adapters = {"beta": {Platform.DISCORD: other_adapter}}
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The default profile's adapter must never receive beta's notification.
    assert default_adapter.sent == [], (
        "Owning-profile subscription must not fall back to the default "
        f"profile's adapter; got {default_adapter.sent!r}"
    )
    assert other_adapter.sent == [], (
        f"beta's discord adapter must not receive a telegram sub; got {other_adapter.sent!r}"
    )
    # The claim is rewound (adapter resolved to None → treated as disconnected),
    # so the event is still unseen and will deliver once beta's adapter connects.
    assert [ev.kind for ev in _unseen_terminal_events_for(tid, "chat-beta")] == ["completed"]


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_injects_internal_session_subscription(tmp_path, monkeypatch):
    """platform=tui + chat_id=agent:* rows wake the owning agent session."""
    db_path = tmp_path / "internal-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    session_key = "agent:main:telegram:dm:chat-1"
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="synthesize helper result", assignee="asuna")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="tui",
            chat_id=session_key,
            notifier_profile="kurumi",
        )
        kb.complete_task(conn, tid, summary="Asuna finished the UX audit.")
    finally:
        conn.close()

    adapter = RecordingHandleAdapter()
    runner = _make_runner(adapter)
    runner._kanban_notifier_profile = "kurumi"
    runner.session_store = SimpleNamespace(
        _entries={
            session_key: SimpleNamespace(
                origin=SessionSource(
                    platform=Platform.TELEGRAM,
                    chat_id="chat-1",
                    chat_type="dm",
                    user_id="user-1",
                    user_name="Haru",
                )
            )
        },
        _ensure_loaded=lambda: None,
    )

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert len(adapter.events) == 1
    assert adapter.events[0].internal is True
    assert "[KANBAN TASK COMPLETE" in adapter.events[0].text
    assert tid in adapter.events[0].text
    assert "Asuna finished the UX audit." in adapter.events[0].text

    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


def test_kanban_notifier_injects_internal_review_and_unblock_events(tmp_path, monkeypatch):
    """Coordinator review and unblock events should wake the orchestrator."""
    db_path = tmp_path / "internal-review-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    session_key = "agent:main:telegram:dm:chat-1"
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="review handoff", assignee="kurumi")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="tui",
            chat_id=session_key,
            notifier_profile="kurumi",
        )
        kb._append_event(
            conn,
            tid,
            "coordinator_review_requested",
            {
                "review_task_id": "t_review01",
                "coordinator": "kairi",
                "reason": "review-required: proof check",
            },
        )
        kb._append_event(conn, tid, "unblocked", {"status": "ready"})
    finally:
        conn.close()

    adapter = RecordingHandleAdapter()
    runner = _make_runner(adapter)
    runner._kanban_notifier_profile = "kurumi"
    runner.session_store = SimpleNamespace(
        _entries={
            session_key: SimpleNamespace(
                origin=SessionSource(
                    platform=Platform.TELEGRAM,
                    chat_id="chat-1",
                    chat_type="dm",
                    user_id="user-1",
                    user_name="Haru",
                )
            )
        },
        _ensure_loaded=lambda: None,
    )

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    texts = [event.text for event in adapter.events]
    assert any("[KANBAN TASK REVIEW REQUESTED" in text for text in texts)
    assert any("Review task: t_review01" in text for text in texts)
    assert any("[KANBAN TASK UNBLOCKED" in text for text in texts)


def test_kanban_notifier_drops_archived_internal_subscription(tmp_path, monkeypatch):
    """Stale archived cards must not resurrect old agent work."""
    db_path = tmp_path / "archived-internal-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    session_key = "agent:main:telegram:dm:chat-1"
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="old archived helper", assignee="kairi")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="tui",
            chat_id=session_key,
            notifier_profile="kurumi",
        )
        kb.complete_task(conn, tid, summary="Old result.")
        assert kb.archive_task(conn, tid)
    finally:
        conn.close()

    adapter = RecordingHandleAdapter()
    runner = _make_runner(adapter)
    runner._kanban_notifier_profile = "kurumi"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.events == []
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()
