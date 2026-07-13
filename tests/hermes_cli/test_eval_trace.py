import json
import sqlite3
import time
from types import SimpleNamespace

from hermes_cli.eval_trace import collect_trace, render_markdown, render_summary


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _make_profile(root, name, *, active_agents=0, user_text="Please continue", assistant_text=None):
    home = root / "profiles" / name
    (home / "logs").mkdir(parents=True)
    (home / "sessions").mkdir(parents=True)
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    now = time.time()
    session_id = f"session-{name}"
    _write_json(
        home / "gateway_state.json",
        {
            "pid": 1234,
            "gateway_state": "running",
            "active_agents": active_agents,
            "platforms": {"telegram": {"state": "connected"}},
            "updated_at": "2026-07-05T00:00:00+00:00",
        },
    )
    _write_json(
        home / "sessions" / "sessions.json",
        {
            "agent:main:telegram:dm:1": {
                "session_key": "agent:main:telegram:dm:1",
                "session_id": session_id,
                "updated_at": "2026-07-05T00:00:00",
                "platform": "telegram",
                "chat_type": "dm",
                "display_name": "Haru",
                "last_prompt_tokens": 1000,
            }
        },
    )
    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, "
        "ended_at REAL, end_reason TEXT, message_count INTEGER, tool_call_count INTEGER, "
        "api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
        "cache_read_tokens INTEGER, cache_write_tokens INTEGER, estimated_cost_usd REAL, "
        "cost_status TEXT, title TEXT)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
        "content TEXT, tool_name TEXT, timestamp REAL, active INTEGER, compacted INTEGER)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, 'telegram', 'test-model', ?, NULL, NULL, 1, 0, 0, 0, 0, 0, 0, 0, 'unknown', NULL)",
        (session_id, now - 60),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active, compacted) "
        "VALUES (?, 'user', ?, NULL, ?, 1, 0)",
        (session_id, user_text, now - 30),
    )
    if assistant_text:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active, compacted) "
            "VALUES (?, 'assistant', ?, NULL, ?, 1, 0)",
            (session_id, assistant_text, now - 10),
        )
    conn.commit()
    conn.close()

    log_stamp = "2026-07-05 00:00:01,000"
    (home / "logs" / "gateway.log").write_text(
        f"{log_stamp} INFO gateway.run: inbound message: platform=telegram user=Haru chat=1 msg='Please continue' reply_to_id=None reply_to_text=''\n"
        f"{log_stamp} WARNING gateway.run: kanban notifier: wakeup injection failed for t_12345678: adapter missing\n",
        encoding="utf-8",
    )


def _make_kanban(root):
    conn = sqlite3.connect(root / "kanban.db")
    conn.execute(
        "CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, status TEXT, session_id TEXT, "
        "current_run_id INTEGER, worker_pid INTEGER, last_heartbeat_at REAL, started_at REAL)"
    )
    conn.execute(
        "INSERT INTO tasks VALUES ('t_1', 'Active task', 'asuna', 'running', 'session-asuna', 9, 222, ?, ?)",
        (time.time(), time.time()),
    )
    conn.commit()
    conn.close()


def test_collect_trace_scores_active_and_idle_profiles(tmp_path):
    _make_profile(tmp_path, "asuna", active_agents=1, assistant_text="Working on it")
    _make_profile(tmp_path, "kairi", active_agents=0, assistant_text="Done")
    _make_kanban(tmp_path)

    trace = collect_trace(
        SimpleNamespace(
            profiles=None,
            since_minutes=120,
            log_lines=20,
            message_limit=5,
        ),
        root=tmp_path,
    )

    assert trace["summary"]["profiles"] == 2
    assert trace["summary"]["active_profiles"] == 1
    profiles = {p["profile"]: p for p in trace["profiles"]}
    assert profiles["asuna"]["evaluation"]["state"] == "active_with_progress"
    assert profiles["asuna"]["evaluation"]["signals"]["active_agents"] == 1
    assert profiles["kairi"]["evaluation"]["state"] == "idle_after_response"
    assert trace["kanban"]["boards"][0]["counts"]["running"] == 1


def test_collect_trace_prefers_human_work_route_over_newer_kanban_route(tmp_path):
    _make_profile(
        tmp_path,
        "rikku",
        active_agents=1,
        user_text="Please power through the rest",
        assistant_text="I am working through the next slice.",
    )
    home = tmp_path / "profiles" / "rikku"
    sessions_path = home / "sessions" / "sessions.json"
    sessions = json.loads(sessions_path.read_text(encoding="utf-8"))
    sessions["agent:main:telegram:group:1:1"] = {
        "session_key": "agent:main:telegram:group:1:1",
        "session_id": "session-rikku-kanban",
        "updated_at": "2026-07-05T00:10:00",
        "platform": "telegram",
        "chat_type": "group",
        "display_name": "Hermes Team",
        "last_prompt_tokens": 2000,
    }
    sessions_path.write_text(json.dumps(sessions), encoding="utf-8")

    conn = sqlite3.connect(home / "state.db")
    now = time.time()
    conn.execute(
        "INSERT INTO sessions VALUES (?, 'telegram', 'test-model', ?, NULL, NULL, 2, 0, 0, 0, 0, 0, 0, 0, 'unknown', NULL)",
        ("session-rikku-kanban", now - 20),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active, compacted) "
        "VALUES (?, 'user', ?, NULL, ?, 1, 0)",
        ("session-rikku-kanban", "[kanban] Task t_12345678 completed: old notification", now - 15),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active, compacted) "
        "VALUES (?, 'assistant', ?, NULL, ?, 1, 0)",
        ("session-rikku-kanban", "Notified.", now - 5),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active, compacted) "
        "VALUES (?, 'user', ?, NULL, ?, 1, 0)",
        (
            "session-rikku",
            "[Your active task list was preserved across context compression]\n- core2-start",
            now - 1,
        ),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active, compacted) "
        "VALUES (?, 'user', ?, NULL, ?, 1, 0)",
        (
            "session-rikku",
            "[ASYNC DELEGATION BATCH COMPLETE - deleg_12345678]\nReviewers finished.",
            now,
        ),
    )
    conn.commit()
    conn.close()

    trace = collect_trace(
        SimpleNamespace(
            profiles=["rikku"],
            since_minutes=120,
            log_lines=20,
            message_limit=5,
        ),
        root=tmp_path,
    )

    profile = trace["profiles"][0]
    assert profile["routes"][0]["chat_type"] == "dm"
    assert profile["routes"][0]["state_db"]["latest_user"]["snippet"] == "Please power through the rest"
    assert profile["routes"][0]["state_db"]["latest_user_raw_is_synthetic"] is True
    assert "Please power through the rest" in render_summary(trace, artifact_paths=None)


def test_collect_trace_surfaces_active_background_subagents(tmp_path):
    _make_profile(tmp_path, "rikku", active_agents=0, assistant_text="Main turn done")
    home = tmp_path / "profiles" / "rikku"
    conn = sqlite3.connect(home / "state.db")
    now = time.time()
    conn.execute(
        "INSERT INTO sessions VALUES (?, 'subagent', 'test-model', ?, NULL, NULL, 3, 2, 1, 10, 2, 0, 0, 0, 'unknown', NULL)",
        ("subagent-active", now - 10),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active, compacted) "
        "VALUES (?, 'tool', ?, 'terminal', ?, 1, 0)",
        ("subagent-active", "review still running", now - 5),
    )
    conn.commit()
    conn.close()

    trace = collect_trace(
        SimpleNamespace(
            profiles=["rikku"],
            since_minutes=120,
            log_lines=20,
            message_limit=5,
        ),
        root=tmp_path,
    )

    profile = trace["profiles"][0]
    assert profile["evaluation"]["state"] == "background_subagents_active"
    assert profile["evaluation"]["signals"]["active_background_sessions"] == 1
    assert trace["summary"]["active_background_profiles"] == 1
    assert "background=1" in render_summary(trace, artifact_paths=None)


def test_render_outputs_include_artifacts_without_secretish_log_values(tmp_path):
    _make_profile(
        tmp_path,
        "rikku",
        active_agents=1,
        user_text="Please power through with token abc",
        assistant_text=None,
    )
    trace = collect_trace(
        SimpleNamespace(
            profiles=["rikku"],
            since_minutes=120,
            log_lines=20,
            message_limit=5,
        ),
        root=tmp_path,
    )

    summary = render_summary(trace, artifact_paths=None)
    markdown = render_markdown(trace)

    assert "rikku" in summary
    assert "active_no_persisted_progress_yet" in summary
    assert "Hermes Eval Trace" in markdown
    assert "Profile Scorecard" in markdown
