import asyncio
import json
import re
import pytest

from pathlib import Path
from types import SimpleNamespace
from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Allow the kanban notifier path-validator to upload artifacts the
    # tests write under ``tmp_path``. Without this, every artifact-delivery
    # test silently drops files because ``tmp_path`` isn't inside the
    # default ``MEDIA_DELIVERY_SAFE_ROOTS`` cache dirs.
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    kb.init_db()
    return home


@pytest.mark.asyncio
async def test_notifier_unsubs_after_completed_event(kanban_home):
    """
    Subscription should be remove after completed event
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="test task", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
        kb.complete_task(conn, tid, result="completed by agent")
    finally:
        conn.close()

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()

    async def _send_and_stop(chat_id, msg, metadata=None):
        runner._running = False

    fake_adapter.send = AsyncMock(side_effect=_send_and_stop)
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    fake_adapter.send.assert_called_once()
    call_msg = fake_adapter.send.call_args[0][1]
    assert "completed" in call_msg

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert subs == [], "Subscription should be unsub after completed event"


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ["gave_up", "crashed", "timed_out"])
async def test_notifier_unsubs_after_abnormal_events(kind, kanban_home):
    """
    Event kinds gave_up / crashed / timed_out send a notification but DO
    NOT delete the subscription. The dispatcher may respawn the task and
    fire the same event kind again (e.g. a worker that crashes, gets
    reclaimed, and crashes a second time); the user must hear about the
    second event too. Subscriptions are removed only when the task hits
    a truly final status (done / archived) — see the comment on
    TERMINAL_KINDS in gateway/run.py and PR #21398.
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    conn = kb.connect()

    try:
        tid = kb.create_task(conn, title=f"test {kind} task", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
        kb._append_event(conn, tid, kind=kind)
    finally:
        conn.close()

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()

    async def _send_and_stop(chat_id, msg, metadata=None):
        runner._running = False

    fake_adapter.send = AsyncMock(side_effect=_send_and_stop)
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    # The user is notified about the abnormal event...
    fake_adapter.send.assert_called_once()
    assert kind.replace('_', ' ') in fake_adapter.send.call_args[0][1]

    # ...but the subscription survives so a respawn-then-same-event cycle
    # reaches the user too. The cursor (last_event_id) advanced inside
    # the same write txn as the claim, so the same event won't re-fire.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert len(subs) == 1, (
        f"Subscription should survive {kind!r} so the next cycle of the "
        f"same event reaches the user; got {subs!r}"
    )
    assert int(subs[0]["last_event_id"]) >= 1, (
        "Cursor should have advanced past the delivered event "
        "(claim_unseen_events_for_sub advances atomically inside the "
        "same write txn as the read)."
    )


@pytest.mark.asyncio
async def test_notifier_second_blocked_delivers(kanban_home):
    """
    After the first blocked, should receive second blocked notification.
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    delivered_msgs: list[str] = []

    async def _capture_send(chat_id, msg, metadata=None):
        delivered_msgs.append(msg)

    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock(side_effect=_capture_send)
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep
    tick_count = 0

    async def _fast_sleep(_):
        nonlocal tick_count
        await _orig_sleep(0)
        tick_count += 1
        if tick_count >= 6:
            runner._running = False

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="test task", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")

        # Cycle 1: blocked for one reason
        kb.block_task(conn, tid, reason="first block", kind="needs_input")
    finally:
        conn.close()

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    # Cycle 2: unblock → block again for a DIFFERENT reason. A distinct
    # block cause must still notify. (A *same*-cause re-block instead trips
    # the unblock-loop breaker and routes to triage — covered by
    # test_kanban_block_kinds.py; here we exercise two genuinely different
    # blocks, which is the case the user wants notified twice.)
    runner._running = True
    tick_count = 0

    conn = kb.connect()
    try:
        kb.unblock_task(conn, tid)
        kb.block_task(conn, tid, reason="second block", kind="capability")
    finally:
        conn.close()

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    blocked_deliveries = [
        m for m in delivered_msgs
        if " blocked" in m and "unblocked" not in m
    ]
    assert "second block" not in blocked_deliveries[0]
    assert "second block" in blocked_deliveries[1]
    assert len(blocked_deliveries) == 2, (
        f"Should receive 2 blocked notification, but only get {len(blocked_deliveries)} count\n"
        f"Message {delivered_msgs}"
    )


# ---------------------------------------------------------------------------
# Regression: gateway watchers must not double-init the kanban DB.
#
# Both the notifier watcher (`_kanban_notifier_watcher`) and the dispatcher
# tick (`_tick_once_for_board`) used to call `_kb.connect(board=slug)`
# immediately followed by `_kb.init_db(board=slug)`. Since `connect()`
# already runs the schema + idempotent migration on first open per process,
# the explicit `init_db()` was redundant — and worse, `init_db()`
# deliberately busts the per-process cache and re-runs the migration on a
# *second* connection, which races the first.  On legacy DBs this surfaced
# as `duplicate column name: <col>` (now tolerated by
# `_add_column_if_missing`) and intermittent `database is locked` errors
# (issue #21378).
#
# The fix removes the `init_db()` calls in both watchers; this regression
# test pins that behaviour so we don't reintroduce them.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notifier_does_not_call_init_db(kanban_home):
    """Notifier watcher path must not invoke `_kb.init_db` (issue #21378)."""
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep
    tick_count = 0

    async def _fast_sleep(_):
        nonlocal tick_count
        await _orig_sleep(0)
        tick_count += 1
        if tick_count >= 3:
            runner._running = False

    init_db_calls: list[object] = []
    real_init_db = kb.init_db

    def _spy_init_db(*args, **kwargs):
        init_db_calls.append((args, kwargs))
        return real_init_db(*args, **kwargs)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep), \
         patch("hermes_cli.kanban_db.init_db", side_effect=_spy_init_db):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    assert init_db_calls == [], (
        "_kanban_notifier_watcher must not call init_db on every tick — "
        "connect() handles first-run schema init. "
        "Reintroducing init_db revives issue #21378. "
        f"Got {len(init_db_calls)} call(s): {init_db_calls}"
    )


def test_dispatcher_tick_does_not_call_init_db(kanban_home, monkeypatch):
    """`_tick_once_for_board` must not invoke `_kb.init_db` (issue #21378).

    `connect()` already runs the schema + idempotent migration on first open
    per process. The explicit `init_db()` call was redundant and triggered a
    second migration on a second connection that raced the first.
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)

    init_db_calls: list[object] = []
    real_init_db = kb.init_db

    def _spy_init_db(*args, **kwargs):
        init_db_calls.append((args, kwargs))
        return real_init_db(*args, **kwargs)

    # The dispatcher watcher's tick lives as a local closure inside
    # `_kanban_dispatcher_watcher`. Read the source and assert the
    # specific patterns that would reintroduce the bug are absent.
    import inspect
    src = inspect.getsource(GatewayRunner._kanban_dispatcher_watcher)
    assert "_kb.init_db(board=slug)" not in src, (
        "_kanban_dispatcher_watcher must not call _kb.init_db(board=slug) — "
        "see issue #21378. Use connect() alone; it runs migrations on first "
        "open per process."
    )

    notifier_src = inspect.getsource(GatewayRunner._kanban_notifier_watcher)
    assert "_kb.init_db(board=slug)" not in notifier_src, (
        "_kanban_notifier_watcher must not call _kb.init_db(board=slug) — "
        "see issue #21378."
    )


@pytest.mark.asyncio
async def test_notifier_skips_subscription_owned_by_other_profile(kanban_home):
    """Each gateway keeps its watcher on, but only the subscribing profile claims."""
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned task", assignee="backend-engineer")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat1",
            notifier_profile="default",
        )
        kb.complete_task(conn, tid, result="done")
    finally:
        conn.close()

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    runner._kanban_notifier_profile = "business-partner"

    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep
    tick_count = 0

    async def _fast_sleep(_):
        nonlocal tick_count
        await _orig_sleep(0)
        tick_count += 1
        if tick_count >= 3:
            runner._running = False

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    fake_adapter.send.assert_not_called()
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert len(subs) == 1
    assert int(subs[0]["last_event_id"]) == 0, "wrong profile must not claim the event"


@pytest.mark.asyncio
async def test_notifier_delivers_subscription_owned_by_current_profile(kanban_home):
    """The gateway for the profile that created/subscribed the task reports it."""
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned task", assignee="backend-engineer")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat1",
            notifier_profile="default",
        )
        kb.complete_task(conn, tid, result="done")
    finally:
        conn.close()

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    runner._kanban_notifier_profile = "default"

    fake_adapter = MagicMock()

    async def _send_and_stop(chat_id, msg, metadata=None):
        runner._running = False

    fake_adapter.send = AsyncMock(side_effect=_send_and_stop)
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    fake_adapter.send.assert_called_once()
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert subs == []


@pytest.mark.asyncio
async def test_gateway_create_autosubscribes_on_explicit_board(kanban_home):
    """`/kanban --board <slug> create ...` must subscribe on that board.

    The gateway handler currently auto-subscribes after `/kanban create`,
    but the create detection must still work when the shared `--board`
    flag appears before the subcommand, and the subscription must land in
    that board's DB rather than the ambient/default board.
    """
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    kb.create_board("projx")

    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="chat1",
        thread_id="th1",
        user_id="u1",
    )
    event = SimpleNamespace(
        text='/kanban --board projx create "hello" --assignee alice',
        source=source,
    )

    out = await GatewayRunner._handle_kanban_command(runner, event)

    assert "subscribed" in out.lower()

    conn = kb.connect(board="projx")
    try:
        subs = kb.list_notify_subs(conn)
        tasks = kb.list_tasks(conn)
    finally:
        conn.close()

    assert [t.title for t in tasks] == ["hello"]
    assert len(subs) == 1
    assert subs[0]["chat_id"] == "chat1"
    assert subs[0]["thread_id"] == "th1"

    conn = kb.connect(board="default")
    try:
        assert kb.list_notify_subs(conn) == []
    finally:
        conn.close()


def test_cli_create_autosubscribes_from_session_env(kanban_home, monkeypatch):
    """Agent shell calls to `hermes kanban create` should notify Telegram too."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat1")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "thread1")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "user1")
    monkeypatch.setenv("HERMES_SESSION_ID", "session-1")
    monkeypatch.setenv("HERMES_PROFILE", "kairi")

    out = kc.run_slash('create "notify shell caller" --assignee kairi')
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        task = kb.get_task(conn, tid)
    finally:
        conn.close()

    assert task.session_id == "session-1"
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "chat1"
    assert subs[0]["thread_id"] == "thread1"
    assert subs[0]["user_id"] == "user1"
    assert subs[0]["notifier_profile"] == "kairi"


def test_cli_create_autosubscribe_preserves_json_output(kanban_home, monkeypatch):
    """Auto-subscribe must not add text to machine-readable create output."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat1")
    monkeypatch.setenv("HERMES_PROFILE", "kairi")

    payload = json.loads(
        kc.run_slash('create "json notify" --assignee kairi --json')
    )

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, payload["id"])
    finally:
        conn.close()

    assert payload["title"] == "json notify"
    assert len(subs) == 1
    assert subs[0]["chat_id"] == "chat1"


def test_autosubscribe_inherits_parent_subscription_without_session_env(
    kanban_home, monkeypatch,
):
    """Child tasks spawned by workers should notify the parent's chat."""
    for name in (
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_THREAD_ID",
        "HERMES_SESSION_USER_ID",
        "HERMES_SESSION_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    from hermes_cli.kanban_notify import maybe_auto_subscribe_task

    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="kairi")
        kb.add_notify_sub(
            conn,
            task_id=parent,
            platform="telegram",
            chat_id="chat1",
            thread_id="thread1",
            user_id="user1",
            notifier_profile="kairi",
        )
        child = kb.create_task(
            conn,
            title="child",
            assignee="asuna",
            parents=(parent,),
            session_id="worker-session",
        )

        assert maybe_auto_subscribe_task(
            conn, child, notifier_profile="kairi"
        )
        subs = kb.list_notify_subs(conn, child)
    finally:
        conn.close()

    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "chat1"
    assert subs[0]["thread_id"] == "thread1"
    assert subs[0]["user_id"] == "user1"
    assert subs[0]["notifier_profile"] == "kairi"


def test_autosubscribe_inherits_parent_session_peer_without_session_env(
    kanban_home, monkeypatch,
):
    """If the parent missed a sub, inherit from another task in its session."""
    for name in (
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_THREAD_ID",
        "HERMES_SESSION_USER_ID",
        "HERMES_SESSION_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    from hermes_cli.kanban_notify import maybe_auto_subscribe_task

    conn = kb.connect()
    try:
        parent = kb.create_task(
            conn,
            title="coordination parent",
            assignee="kairi",
            session_id="telegram-session",
        )
        peer = kb.create_task(
            conn,
            title="same chat peer",
            assignee="asuna",
            session_id="telegram-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=peer,
            platform="telegram",
            chat_id="chat1",
            thread_id="thread1",
            user_id="user1",
            notifier_profile="kairi",
        )
        child = kb.create_task(
            conn,
            title="worker child",
            assignee="kurumi",
            parents=(parent,),
            session_id="worker-session",
        )

        assert maybe_auto_subscribe_task(
            conn, child, notifier_profile="kairi"
        )
        subs = kb.list_notify_subs(conn, child)
    finally:
        conn.close()

    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "chat1"
    assert subs[0]["thread_id"] == "thread1"
    assert subs[0]["user_id"] == "user1"
    assert subs[0]["notifier_profile"] == "kairi"


def test_autosubscribe_recovers_internal_route_from_recorded_session_id(
    kanban_home, monkeypatch,
):
    """Agent-created root tasks may only retain tasks.session_id.

    The gateway ContextVars are not always present in tool subprocesses, so
    recover the persistent session key from profiles/*/sessions/sessions.json
    and create an internal wake subscription for the orchestrator.
    """
    for name in (
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_THREAD_ID",
        "HERMES_SESSION_USER_ID",
        "HERMES_SESSION_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    session_key = "agent:main:telegram:dm:chat1"
    session_dir = kanban_home / "profiles" / "kurumi" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": "sess-kurumi-1",
                    "origin": {
                        "platform": "telegram",
                        "chat_id": "chat1",
                        "chat_type": "dm",
                        "user_id": "user1",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    from hermes_cli.kanban_notify import maybe_auto_subscribe_task

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="root helper",
            assignee="asuna",
            created_by="kurumi",
            session_id="sess-kurumi-1",
        )
        assert maybe_auto_subscribe_task(conn, tid, notifier_profile="kurumi")
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()

    assert len(subs) == 1
    assert subs[0]["platform"] == "tui"
    assert subs[0]["chat_id"] == session_key
    assert subs[0]["user_id"] == "user1"
    assert subs[0]["notifier_profile"] == "kurumi"


def test_recorded_session_prefers_valid_route_profile_over_invalid_author(
    kanban_home, monkeypatch,
):
    """OS usernames and invalid explicit owners must not orphan subscriptions."""
    for name in (
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_THREAD_ID",
        "HERMES_SESSION_USER_ID",
        "HERMES_SESSION_KEY",
        "HERMES_KANBAN_NOTIFIER_PROFILE",
        "HERMES_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)

    session_key = "agent:main:telegram:dm:chat-route"
    session_dir = kanban_home / "profiles" / "kurumi" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": "sess-route-1",
                    "origin": {
                        "platform": "telegram",
                        "chat_id": "chat-route",
                        "chat_type": "dm",
                        "user_id": "user-route",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    from hermes_cli.kanban_notify import maybe_auto_subscribe_task

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="route-owned helper",
            assignee="asuna",
            created_by="dayvo",
            session_id="sess-route-1",
        )
        assert maybe_auto_subscribe_task(
            conn,
            tid,
            notifier_profile="dayvo",
        )
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()

    assert len(subs) == 1
    assert subs[0]["notifier_profile"] == "kurumi"


def test_autosubscribe_recovers_internal_route_from_compacted_ancestor_session(
    kanban_home, monkeypatch,
):
    """Tasks created before compression should still wake the current chat."""
    for name in (
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_THREAD_ID",
        "HERMES_SESSION_USER_ID",
        "HERMES_SESSION_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    session_key = "agent:main:telegram:dm:chat1"
    profile_root = kanban_home / "profiles" / "kurumi"
    session_dir = profile_root / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / "sessions.json").write_text(
        json.dumps(
            {
                session_key: {
                    "session_key": session_key,
                    "session_id": "sess-kurumi-current",
                    "origin": {
                        "platform": "telegram",
                        "chat_id": "chat1",
                        "chat_type": "dm",
                        "user_id": "user1",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    import sqlite3

    state_db = profile_root / "state.db"
    conn_state = sqlite3.connect(state_db)
    try:
        conn_state.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)"
        )
        conn_state.execute(
            "INSERT INTO sessions (id, parent_session_id) VALUES (?, ?)",
            ("sess-kurumi-ancestor", None),
        )
        conn_state.execute(
            "INSERT INTO sessions (id, parent_session_id) VALUES (?, ?)",
            ("sess-kurumi-current", "sess-kurumi-ancestor"),
        )
        conn_state.commit()
    finally:
        conn_state.close()

    from hermes_cli.kanban_notify import maybe_auto_subscribe_task

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="root helper from old session",
            assignee="asuna",
            created_by="kurumi",
            session_id="sess-kurumi-ancestor",
        )
        assert maybe_auto_subscribe_task(conn, tid, notifier_profile="kurumi")
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()

    assert len(subs) == 1
    assert subs[0]["platform"] == "tui"
    assert subs[0]["chat_id"] == session_key
    assert subs[0]["user_id"] == "user1"
    assert subs[0]["notifier_profile"] == "kurumi"


@pytest.mark.asyncio
async def test_notifier_uploads_artifacts_on_completion(kanban_home, tmp_path, monkeypatch):
    """When a completed event carries ``artifacts`` in its payload, the
    notifier uploads each file to the subscribed chat as a native
    attachment. Images batch through send_multiple_images; documents
    route through send_document. See the artifacts wiring in
    gateway/run.py._deliver_kanban_artifacts.
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from tools import kanban_tools as kt

    # ``_deliver_kanban_artifacts`` routes candidates through
    # ``BasePlatformAdapter.filter_local_delivery_paths``, which only accepts
    # paths under ``MEDIA_DELIVERY_SAFE_ROOTS`` or roots explicitly allowlisted
    # via ``HERMES_MEDIA_ALLOW_DIRS``. Test fixtures live under ``tmp_path``,
    # so allowlist it for the duration of the test.
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))

    # Materialize real files so os.path.isfile passes inside the helper.
    chart_path = tmp_path / "q3-revenue.png"
    chart_path.write_bytes(b"PNG-fake-bytes")
    report_path = tmp_path / "report.pdf"
    report_path.write_bytes(b"%PDF-fake")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="render q3 chart", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
    finally:
        conn.close()

    # Use the production handler so we exercise the full path: tool args
    # → metadata.artifacts → event payload promotion.
    import os
    os.environ["HERMES_KANBAN_TASK"] = tid
    try:
        out = kt._handle_complete({
            "summary": "rendered the chart",
            "artifacts": [str(chart_path), str(report_path)],
        })
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)
    import json as _json
    assert _json.loads(out)["ok"] is True

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()
    fake_adapter.name = "telegram"

    sends: list = []
    images_uploaded: list = []
    documents_uploaded: list = []

    async def _send(chat_id, msg, metadata=None):
        sends.append((chat_id, msg))
        runner._running = False

    async def _send_images(chat_id, images, metadata=None, **_kw):
        images_uploaded.extend(p for p, _ in images)

    async def _send_document(chat_id, file_path, metadata=None, **_kw):
        documents_uploaded.append(file_path)

    fake_adapter.send = AsyncMock(side_effect=_send)
    fake_adapter.send_multiple_images = AsyncMock(side_effect=_send_images)
    fake_adapter.send_document = AsyncMock(side_effect=_send_document)
    # extract_local_files is used internally for legacy path fallback;
    # the real BasePlatformAdapter implementation lives there, so wire it.
    from gateway.platforms.base import BasePlatformAdapter
    fake_adapter.extract_local_files = BasePlatformAdapter.extract_local_files

    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    # The text completion notification fired.
    assert len(sends) == 1
    # The PNG rode the image-batch path.
    assert any("q3-revenue.png" in p for p in images_uploaded), images_uploaded
    # The PDF rode the document path.
    assert any("report.pdf" in p for p in documents_uploaded), documents_uploaded


@pytest.mark.asyncio
async def test_notifier_artifact_delivery_skips_missing_files(kanban_home, tmp_path, monkeypatch):
    """Missing artifact paths are silently skipped — they may have been
    referenced by name only. The notifier must not crash and must still
    deliver any artifacts that do exist."""
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from tools import kanban_tools as kt

    # Allow ``tmp_path`` through the media-delivery safety filter. See the
    # companion test for the full explanation.
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))

    real_pdf = tmp_path / "real.pdf"
    real_pdf.write_bytes(b"%PDF-fake")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
    finally:
        conn.close()

    import os
    os.environ["HERMES_KANBAN_TASK"] = tid
    try:
        kt._handle_complete({
            "summary": "one real, one ghost",
            "artifacts": [str(real_pdf), "/tmp/definitely-does-not-exist.pdf"],
        })
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()
    fake_adapter.name = "telegram"

    documents_uploaded: list = []

    async def _send(chat_id, msg, metadata=None):
        runner._running = False

    async def _send_document(chat_id, file_path, metadata=None, **_kw):
        documents_uploaded.append(file_path)

    fake_adapter.send = AsyncMock(side_effect=_send)
    fake_adapter.send_document = AsyncMock(side_effect=_send_document)
    fake_adapter.send_multiple_images = AsyncMock()
    from gateway.platforms.base import BasePlatformAdapter
    fake_adapter.extract_local_files = BasePlatformAdapter.extract_local_files

    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    # Only the real file was uploaded.
    assert len(documents_uploaded) == 1
    assert "real.pdf" in documents_uploaded[0]
