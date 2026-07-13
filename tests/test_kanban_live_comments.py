"""Behavior tests for live coordinator-comment delivery to workers."""

import json
import os
import sqlite3
from unittest.mock import patch

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


def test_external_comment_repeats_until_lifecycle_ack(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "sol", "New acceptance gate")
        _worker_env(monkeypatch, path)

        with patch("model_tools.registry.dispatch", return_value='{"ok":true}'):
            first = json.loads(handle_function_call("web_search", {"q": "x"}))
            second = json.loads(handle_function_call("web_search", {"q": "y"}))

            for result in (first, second):
                update = result["_kanban_coordinator_updates"]
                assert update["comments"] == [{
                    "id": comment_id,
                    "author": "sol",
                    "body": "New acceptance gate",
                    "created_at": 123,
                }]
                assert "kanban_complete" in update["instruction"]

            assert os.environ["HERMES_KANBAN_COMMENT_CURSOR"] == "0"
            assert int(
                os.environ["HERMES_KANBAN_COMMENT_DELIVERED_CURSOR"]
            ) == comment_id
    finally:
        conn.close()


def test_heartbeat_acknowledges_delivered_comment(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "sol", "Use exact head")
        _worker_env(monkeypatch, path)

        with patch("model_tools.registry.dispatch", return_value='{"ok":true}'):
            delivered = json.loads(handle_function_call("web_search", {"q": "x"}))
            assert "_kanban_coordinator_updates" in delivered
            acknowledged = json.loads(handle_function_call("kanban_heartbeat", {}))

        assert acknowledged == {"ok": True}
        assert int(os.environ["HERMES_KANBAN_COMMENT_CURSOR"]) == comment_id
    finally:
        conn.close()


def test_failed_heartbeat_does_not_acknowledge(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        _insert_comment(conn, "t_live", "sol", "Use exact head")
        _worker_env(monkeypatch, path)

        with patch("model_tools.registry.dispatch", return_value='{"ok":true}'):
            handle_function_call("web_search", {"q": "x"})
        with patch(
            "model_tools.registry.dispatch",
            return_value='{"error":"heartbeat rejected"}',
        ):
            failed = json.loads(handle_function_call("kanban_heartbeat", {}))

        assert failed["error"] == "heartbeat rejected"
        assert "_kanban_coordinator_updates" in failed
        assert os.environ["HERMES_KANBAN_COMMENT_CURSOR"] == "0"
    finally:
        conn.close()


def test_same_author_is_not_treated_as_worker_identity(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "KURUMI", "operator update")
        _worker_env(monkeypatch, path)

        with patch("model_tools.registry.dispatch", return_value='{"ok":true}'):
            result = json.loads(handle_function_call("web_search", {"q": "x"}))

        comments = result["_kanban_coordinator_updates"]["comments"]
        assert [comment["id"] for comment in comments] == [comment_id]
        assert os.environ["HERMES_KANBAN_COMMENT_CURSOR"] == "0"
    finally:
        conn.close()


def test_exact_successful_comment_call_skips_only_its_own_row(monkeypatch, tmp_path):
    path, conn = _comment_db(tmp_path)
    try:
        comment_id = _insert_comment(conn, "t_live", "kurumi", "my evidence")
        _worker_env(monkeypatch, path)
        result_payload = json.dumps({
            "ok": True,
            "task_id": "t_live",
            "comment_id": comment_id,
        })

        with patch("model_tools.registry.dispatch", return_value=result_payload):
            result = json.loads(handle_function_call(
                "kanban_comment", {"task_id": "t_live", "body": "my evidence"}
            ))

        assert result == json.loads(result_payload)
        assert int(os.environ["HERMES_KANBAN_COMMENT_CURSOR"]) == comment_id
    finally:
        conn.close()


def test_missing_comment_db_fails_open(monkeypatch, tmp_path):
    _worker_env(monkeypatch, tmp_path / "missing.db")
    with patch("model_tools.registry.dispatch", return_value='{"ok":true}'):
        result = handle_function_call("web_search", {"q": "x"})
    assert result == '{"ok":true}'
