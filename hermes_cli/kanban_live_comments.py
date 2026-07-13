"""Deliver coordinator comments to the owning kanban worker agent.

The dispatcher records an immutable comment baseline in the worker process
environment. Mutable delivery state belongs to the top-level ``AIAgent``
instance, not ``os.environ``: delegated agents and parallel tool threads share
one process, so process-global cursors would let one execution context
acknowledge comments another context never saw.

The owning agent binds its :class:`CommentDeliveryState` around tool dispatch.
Comments added after the spawn baseline are appended to tool results, which
preserves message-role alternation and prompt caching. Delivery fails open for
ordinary tools, while scoped completion reads the acknowledged cursor and
fails closed when that cursor is missing or malformed.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


logger = logging.getLogger(__name__)

COMMENT_CURSOR_ENV = "HERMES_KANBAN_COMMENT_CURSOR"
COMMENT_DELIVERED_CURSOR_ENV = "HERMES_KANBAN_COMMENT_DELIVERED_CURSOR"

_ACK_TOOLS = frozenset({"kanban_comment", "kanban_heartbeat"})
_UPDATE_KEY = "_kanban_coordinator_updates"
_UPDATE_INSTRUCTION = (
    "Coordinator comments arrived after this worker run started. Treat them "
    "as current task requirements. Acknowledge them with kanban_heartbeat or "
    "kanban_comment before attempting kanban_complete; completion remains "
    "blocked until that acknowledgement tool call."
)


def _cursor_value(raw: Any) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


@dataclass
class CommentDeliveryState:
    """Mutable cursor state owned by exactly one top-level agent session."""

    task_id: str
    db_path: str
    run_id: str
    owner_session_id: str
    acknowledged_cursor: int
    delivered_cursor: int
    delivery_epoch: str = ""
    acknowledgeable_cursor: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


_ACTIVE_STATE: ContextVar[CommentDeliveryState | None] = ContextVar(
    "HERMES_KANBAN_COMMENT_DELIVERY_STATE",
    default=None,
)


def build_comment_delivery_state(owner_session_id: str) -> CommentDeliveryState | None:
    """Build the owning agent's state from the dispatcher's spawn baseline."""
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    db_path = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if not task_id or not db_path or not str(owner_session_id or "").strip():
        return None
    try:
        canonical_db = str(Path(db_path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        canonical_db = db_path
    acknowledged = _cursor_value(os.environ.get(COMMENT_CURSOR_ENV))
    delivered = max(
        acknowledged,
        _cursor_value(os.environ.get(COMMENT_DELIVERED_CURSOR_ENV)),
    )
    return CommentDeliveryState(
        task_id=task_id,
        db_path=canonical_db,
        run_id=os.environ.get("HERMES_KANBAN_RUN_ID", "").strip(),
        owner_session_id=str(owner_session_id).strip(),
        acknowledged_cursor=acknowledged,
        delivered_cursor=delivered,
        acknowledgeable_cursor=acknowledged,
    )


@contextmanager
def bind_comment_delivery_state(
    state: CommentDeliveryState | None,
    *,
    session_id: str = "",
) -> Iterator[None]:
    """Bind an owner state around one model-tool dispatch.

    ``None`` deliberately leaves an already-bound state untouched so the
    deferred ``tool_call`` bridge can recurse into ``handle_function_call``.
    A state is otherwise accepted only for its owning session.
    """
    if state is None:
        yield
        return
    if not session_id or str(session_id) != state.owner_session_id:
        yield
        return
    token = _ACTIVE_STATE.set(state)
    try:
        yield
    finally:
        _ACTIVE_STATE.reset(token)


def current_acknowledged_cursor(task_id: str) -> int | None:
    """Return the active owner's cursor, failing closed for scoped workers."""
    state = _ACTIVE_STATE.get()
    if state is not None and state.task_id == task_id:
        with state.lock:
            return state.acknowledged_cursor
    if os.environ.get("HERMES_KANBAN_TASK", "").strip() != task_id:
        return None
    # A dispatcher-scoped worker must never lose its completion gate merely
    # because spawn-time DB access failed or an inherited cursor was malformed.
    return _cursor_value(os.environ.get(COMMENT_CURSOR_ENV))


def _turn_epoch(turn_id: str, api_request_id: str) -> str:
    return str(turn_id or api_request_id or "").strip()


def acknowledge_delivered_comments(
    function_name: str,
    result: str,
    *,
    turn_id: str = "",
    api_request_id: str = "",
) -> None:
    """A successful lifecycle call acknowledges only previously seen rows."""
    state = _ACTIVE_STATE.get()
    if state is None or function_name not in _ACK_TOOLS:
        return
    try:
        parsed = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        return

    epoch = _turn_epoch(turn_id, api_request_id)
    with state.lock:
        target = state.acknowledgeable_cursor
        if state.delivery_epoch and state.delivery_epoch != epoch:
            target = max(target, state.delivered_cursor)
        if target > state.acknowledged_cursor:
            state.acknowledged_cursor = target


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
    """Return the exact comment created by this successful call, if proven."""
    if function_name != "kanban_comment":
        return None
    try:
        parsed = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(parsed, dict)
        or parsed.get("ok") is not True
        or str(parsed.get("task_id") or "") != task_id
    ):
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


def _record_delivery(
    state: CommentDeliveryState,
    max_seen: int,
    epoch: str,
) -> None:
    if max_seen <= state.delivered_cursor:
        return
    if state.delivery_epoch and state.delivery_epoch != epoch:
        state.acknowledgeable_cursor = max(
            state.acknowledgeable_cursor,
            state.delivered_cursor,
        )
    state.delivered_cursor = max_seen
    state.delivery_epoch = epoch


def inject_pending_comments(
    result: str,
    *,
    function_name: str = "",
    turn_id: str = "",
    api_request_id: str = "",
) -> str:
    """Append unseen external comments to a tool result exactly at return."""
    state = _ACTIVE_STATE.get()
    if state is None:
        return result
    epoch = _turn_epoch(turn_id, api_request_id)

    try:
        with state.lock:
            rows = _pending_rows(
                state.task_id,
                state.db_path,
                state.acknowledged_cursor,
            )
            if not rows:
                return result

            max_seen = max(int(row["id"]) for row in rows)
            self_comment_id = _self_comment_id(result, function_name, state.task_id)
            external = [
                row for row in rows
                if int(row["id"]) != self_comment_id
            ]
            _record_delivery(state, max_seen, epoch)

            if not external:
                # The exact successful kanban_comment response proves this
                # call created every pending row. Advancing cannot hide an
                # external coordinator update because none was filtered out.
                state.acknowledged_cursor = max(
                    state.acknowledged_cursor,
                    max_seen,
                )
                state.acknowledgeable_cursor = max(
                    state.acknowledgeable_cursor,
                    state.acknowledged_cursor,
                )
                return result
            return _append_update_payload(result, external)
    except Exception as exc:
        logger.debug("kanban live-comment delivery failed: %s", exc)
        return result
