"""Behavior tests for live coordinator-comment delivery to workers."""

import json
import os
import sqlite3
from unittest.mock import patch

from hermes_cli.kanban_live_comments import build_comment_delivery_state
from model_tools import handle_function_call


def _comment_db(tmp_path):
    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE task_comments ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
        "author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    conn.commit()
    return path, conn


def _insert_comment(conn, task_id, author, body):
    cur = conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, ?, ?, 123)",
        (task_id, author, body),
    )
    conn.commit()
    return int(cur.lastrowid)


def _worker_env(monkeypatch, db_path, *, cursor="0"):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_live")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_PROFILE", "kurumi")
    monkeypatch.setenv("HERMES_KANBAN_COMMENT_CURSOR", cursor)
    monkeypatch.setenv("HERMES_KANBAN_COMMENT_DELIVERED_CURSOR", cursor)
    return build_comment_delivery_state("owner-session")


def _call(
    state,
    name,
    args,
    *,
    turn="turn-1",
    api_request="",
    result=None,
    session="owner-session",
):
    dispatch = patch("model_tools.registry.dispatch", return_value=result or '{"ok":true}')
    with dispatch:
        return handle_function_call(
            name,
            args,
            session_id=session,
            turn_id=turn,
            api_request_id=api_request,
            kanban_comment_state=state,
        )


def test_external_comment_repeats_until_lifecycle_ack(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "sol", "New acceptance gate")
        state = _worker_env(monkeypatch, path)

        first = json.loads(_call(state, "web_search", {"q": "x"}, turn="turn-1"))
        second = json.loads(_call(state, "web_search", {"q": "y"}, turn="turn-2"))

        for result in (first, second):
            update = result["_kanban_coordinator_updates"]
            assert update["comments"] == [{
                "id": comment_id,
                "author": "sol",
                "body": "New acceptance gate",
                "created_at": 123,
            }]
            assert "kanban_complete" in update["instruction"]

        assert state.acknowledged_cursor == 0
        assert state.delivered_cursor == comment_id
        assert os.environ["HERMES_KANBAN_COMMENT_CURSOR"] == "0"
    finally:
        conn.close()


def test_heartbeat_acknowledges_delivered_comment(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "sol", "Use exact head")
        state = _worker_env(monkeypatch, path)

        delivered = json.loads(_call(
            state, "web_search", {"q": "x"}, turn="turn-1"
        ))
        assert "_kanban_coordinator_updates" in delivered
        acknowledged = json.loads(_call(
            state, "kanban_heartbeat", {}, turn="turn-2"
        ))

        assert acknowledged == {"ok": True}
        assert state.acknowledged_cursor == comment_id
        assert os.environ["HERMES_KANBAN_COMMENT_CURSOR"] == "0"
    finally:
        conn.close()


def test_failed_heartbeat_does_not_acknowledge(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        _insert_comment(conn, "t_live", "sol", "Use exact head")
        state = _worker_env(monkeypatch, path)

        _call(state, "web_search", {"q": "x"}, turn="turn-1")
        failed = json.loads(_call(
            state,
            "kanban_heartbeat",
            {},
            turn="turn-2",
            result='{"error":"heartbeat rejected"}',
        ))

        assert failed["error"] == "heartbeat rejected"
        assert "_kanban_coordinator_updates" in failed
        assert state.acknowledged_cursor == 0
    finally:
        conn.close()


def test_same_author_is_not_treated_as_worker_identity(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "KURUMI", "operator update")
        state = _worker_env(monkeypatch, path)

        result = json.loads(_call(state, "web_search", {"q": "x"}))

        comments = result["_kanban_coordinator_updates"]["comments"]
        assert [comment["id"] for comment in comments] == [comment_id]
        assert state.acknowledged_cursor == 0
    finally:
        conn.close()


def test_exact_successful_comment_call_skips_only_its_own_row(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "kurumi", "my evidence")
        state = _worker_env(monkeypatch, path)
        result_payload = json.dumps({
            "ok": True,
            "task_id": "t_live",
            "comment_id": comment_id,
        })

        result = json.loads(_call(
            state,
            "kanban_comment",
            {"task_id": "t_live", "body": "my evidence"},
            result=result_payload,
        ))

        assert result == json.loads(result_payload)
        assert state.acknowledged_cursor == comment_id
    finally:
        conn.close()


def test_missing_comment_db_fails_open(monkeypatch, tmp_path):
    state = _worker_env(monkeypatch, tmp_path / "missing.db")
    result = _call(state, "web_search", {"q": "x"})
    assert result == '{"ok":true}'


def test_failed_comment_response_cannot_skip_matching_row(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "sol", "must remain visible")
        state = _worker_env(monkeypatch, path)
        rejected = json.dumps({
            "ok": False,
            "error": "rejected",
            "task_id": "t_live",
            "comment_id": comment_id,
        })

        result = json.loads(_call(
            state,
            "kanban_comment",
            {"task_id": "t_live", "body": "failed"},
            result=rejected,
        ))

        assert result["ok"] is False
        assert [
            item["id"]
            for item in result["_kanban_coordinator_updates"]["comments"]
        ] == [comment_id]
        assert state.acknowledged_cursor == 0
    finally:
        conn.close()


def test_owner_and_subagent_sessions_cannot_ack_each_other(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "sol", "owner-only update")
        owner_state = _worker_env(monkeypatch, path)

        owner_result = json.loads(_call(
            owner_state, "web_search", {"q": "x"}, turn="owner-turn-1"
        ))
        assert "_kanban_coordinator_updates" in owner_result

        # Passing the owner's state with a different session id must not bind
        # it. This models a delegated AIAgent, which owns no comment state.
        child_result = json.loads(_call(
            owner_state,
            "kanban_heartbeat",
            {},
            turn="child-turn-1",
            session="child-session",
        ))
        assert child_result == {"ok": True}
        assert owner_state.acknowledged_cursor == 0

        repeated = json.loads(_call(
            owner_state, "web_search", {"q": "y"}, turn="owner-turn-2"
        ))
        assert [
            item["id"]
            for item in repeated["_kanban_coordinator_updates"]["comments"]
        ] == [comment_id]
    finally:
        conn.close()


def test_api_epoch_fences_parallel_ack_but_allows_next_iteration(
    monkeypatch, tmp_path
):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "sol", "same-turn update")
        state = _worker_env(monkeypatch, path)

        delivered = json.loads(_call(
            state,
            "web_search",
            {"q": "x"},
            turn="worker-turn",
            api_request="worker-turn:api:0",
        ))
        assert "_kanban_coordinator_updates" in delivered
        _call(
            state,
            "kanban_heartbeat",
            {},
            turn="worker-turn",
            api_request="worker-turn:api:0",
        )
        assert state.acknowledged_cursor == 0

        _call(
            state,
            "kanban_heartbeat",
            {},
            turn="worker-turn",
            api_request="worker-turn:api:1",
        )
        assert state.acknowledged_cursor == comment_id
    finally:
        conn.close()


def test_tool_call_bridge_preserves_api_delivery_epoch(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "sol", "bridge update")
        state = _worker_env(monkeypatch, path)
        tool_defs = [{
            "type": "function",
            "function": {"name": "deferred_probe", "parameters": {}},
        }]

        with (
            patch("model_tools.get_tool_definitions", return_value=tool_defs),
            patch(
                "tools.tool_search.is_deferrable_tool_name",
                side_effect=lambda name: name == "deferred_probe",
            ),
        ):
            delivered = json.loads(_call(
                state,
                "tool_call",
                {"name": "deferred_probe", "arguments": {}},
                turn="worker-turn",
                api_request="worker-turn:api:0",
            ))

        assert "_kanban_coordinator_updates" in delivered
        assert state.delivery_epoch == "worker-turn:api:0"
        _call(
            state,
            "kanban_heartbeat",
            {},
            turn="worker-turn",
            api_request="worker-turn:api:1",
        )
        assert state.acknowledged_cursor == comment_id
    finally:
        conn.close()


def test_self_comment_skips_only_own_row_among_external_updates(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        first = _insert_comment(conn, "t_live", "sol", "before")
        own = _insert_comment(conn, "t_live", "kurumi", "my evidence")
        last = _insert_comment(conn, "t_live", "sol", "after")
        state = _worker_env(monkeypatch, path)
        response = json.dumps({
            "ok": True,
            "task_id": "t_live",
            "comment_id": own,
        })

        result = json.loads(_call(
            state,
            "kanban_comment",
            {"task_id": "t_live", "body": "my evidence"},
            turn="turn-1",
            result=response,
        ))
        assert [
            item["id"]
            for item in result["_kanban_coordinator_updates"]["comments"]
        ] == [first, last]
        assert state.acknowledged_cursor == 0

        _call(state, "kanban_heartbeat", {}, turn="turn-2")
        assert state.acknowledged_cursor == last
    finally:
        conn.close()
