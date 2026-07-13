"""Deliver coordinator comments to running kanban workers.

Dispatcher workers receive their task's current comment id when they start.
Comments added later are appended to ordinary tool results, which preserves
message-role alternation and prompt caching while still reaching the model at
the next tool boundary.  Delivery fails open: a missing, busy, or unreadable
board must never break the tool the worker actually called.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

COMMENT_CURSOR_ENV = "HERMES_KANBAN_COMMENT_CURSOR"
COMMENT_DELIVERED_CURSOR_ENV = "HERMES_KANBAN_COMMENT_DELIVERED_CURSOR"

_ACK_TOOLS = frozenset({"kanban_comment", "kanban_heartbeat"})
_CURSOR_LOCK = threading.RLock()
_UPDATE_KEY = "_kanban_coordinator_updates"
_UPDATE_INSTRUCTION = (
    "Coordinator comments arrived after this worker run started. Treat them "
    "as current task requirements. Acknowledge them with kanban_heartbeat or "
    "kanban_comment before attempting kanban_complete; completion remains "
    "blocked until that acknowledgement tool call."
)


def _cursor(name: str) -> int:
    try:
        return max(0, int(os.environ.get(name, "0")))
    except (TypeError, ValueError):
        return 0


def acknowledge_delivered_comments(function_name: str, result: str) -> None:
    """A successful lifecycle call acknowledges the delivered cursor."""
    if function_name not in _ACK_TOOLS:
        return
    try:
        parsed = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        return
    with _CURSOR_LOCK:
        delivered = _cursor(COMMENT_DELIVERED_CURSOR_ENV)
        if delivered > _cursor(COMMENT_CURSOR_ENV):
            os.environ[COMMENT_CURSOR_ENV] = str(delivered)


def _pending_rows(task_id: str, db_path: str, cursor: int) -> list[dict[str, Any]]:
    path = Path(db_path).expanduser()
    if not path.is_file():
        return []
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, author, body, created_at FROM task_comments "
            "WHERE task_id = ? AND id > ? ORDER BY id ASC",
            (task_id, cursor),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _self_comment_id(result: str, function_name: str, task_id: str) -> int | None:
    """Return the exact comment created by this call, when provable.

    Author names are not identities: an operator can legitimately post as the
    same profile that owns a worker. Only a successful ``kanban_comment``
    result for this exact task proves that a row came from the current call.
    """
    if function_name != "kanban_comment":
        return None
    try:
        parsed = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or str(parsed.get("task_id") or "") != task_id:
        return None
    try:
        return int(parsed["comment_id"])
    except (KeyError, TypeError, ValueError):
        return None


def _append_update_payload(result: str, comments: list[dict[str, Any]]) -> str:
    payload = {
        "instruction": _UPDATE_INSTRUCTION,
        "comments": [
            {
                "id": int(comment["id"]),
                "author": str(comment.get("author") or ""),
                "body": str(comment.get("body") or ""),
                "created_at": int(comment.get("created_at") or 0),
            }
            for comment in comments
        ],
    }
    try:
        parsed = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return f"{result}\n\n{_UPDATE_KEY}: {json.dumps(payload, ensure_ascii=False)}"
    if isinstance(parsed, dict):
        parsed[_UPDATE_KEY] = payload
        return json.dumps(parsed, ensure_ascii=False)
    return json.dumps(
        {"tool_result": parsed, _UPDATE_KEY: payload},
        ensure_ascii=False,
    )


def inject_pending_comments(result: str, *, function_name: str = "") -> str:
    """Append unseen external task comments to ``result`` exactly at return.

    External comments remain pending until a later successful
    heartbeat/comment call acknowledges the delivered cursor. The exact row
    proven to have been created by the current comment call is skipped; an
    author-name match alone never suppresses an operator comment.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    db_path = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if not task_id or not db_path:
        return result

    try:
        with _CURSOR_LOCK:
            acknowledged = _cursor(COMMENT_CURSOR_ENV)
            rows = _pending_rows(task_id, db_path, acknowledged)
            if not rows:
                return result

            max_seen = max(int(row["id"]) for row in rows)
            self_comment_id = _self_comment_id(result, function_name, task_id)
            external = [
                row for row in rows
                if int(row["id"]) != self_comment_id
            ]
            delivered = max(max_seen, _cursor(COMMENT_DELIVERED_CURSOR_ENV))
            os.environ[COMMENT_DELIVERED_CURSOR_ENV] = str(delivered)

            if not external:
                # The exact successful kanban_comment response proves this
                # call created the only new row. Skipping it is safe and
                # prevents an acknowledgement loop.
                os.environ[COMMENT_CURSOR_ENV] = str(max_seen)
                return result
            return _append_update_payload(result, external)
    except Exception as exc:
        logger.debug("kanban live-comment delivery failed: %s", exc)
        return result
