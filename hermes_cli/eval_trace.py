"""Local eval trace collector for Hermes gateways and profile sessions.

The trace is intentionally provider-agnostic and read-only. It joins the
durable sources Hermes already owns: gateway state files, profile session
indexes, state.db messages, kanban board DBs, and recent gateway logs.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import pathname2url

from hermes_constants import get_default_hermes_root

_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
_INBOUND_RE = re.compile(r"inbound message: .*? msg=(?P<msg>'.*?'|\".*?\")")
_RESPONSE_RE = re.compile(
    r"response ready: .*? time=(?P<seconds>[0-9.]+)s api_calls=(?P<api_calls>\d+) response=(?P<chars>\d+) chars"
)
_KANBAN_EVENT_RE = re.compile(r"\[kanban\] Task (?P<task_id>t_[0-9a-f]+) (?P<state>\w+)")
_ASYNC_BATCH_RE = re.compile(r"\[ASYNC DELEGATION BATCH COMPLETE")
_SECRETISH_RE = re.compile(
    r"(?i)(bot\d*token|telegram.*token|api[_-]?key|authorization|bearer\s+[a-z0-9._-]+)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch_to_iso(value: Any) -> str | None:
    try:
        if value is None:
            return None
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _parse_iso_epoch(value: Any) -> float:
    if not value:
        return 0.0
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, OSError):
        return 0.0


def _parse_log_epoch(line: str) -> float | None:
    match = _LOG_TS_RE.match(line)
    if not match:
        return None
    try:
        dt = datetime.strptime(
            f"{match.group(1)}.{match.group(2)}", "%Y-%m-%d %H:%M:%S.%f"
        )
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _redact(text: Any, *, limit: int = 500) -> str:
    if text is None:
        return ""
    value = str(text)
    try:
        from agent.redact import redact_sensitive_text

        value = redact_sensitive_text(value, force=True)
    except Exception:
        value = _SECRETISH_RE.sub("[REDACTED]", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) > limit:
        return value[: max(0, limit - 1)].rstrip() + "…"
    return value


def _decode_content(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (list, dict)):
        return json.dumps(raw, ensure_ascii=False)
    text = str(raw)
    # Many tool messages are JSON blobs. Keep them readable without failing
    # when the content is normal prose.
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.dumps(json.loads(stripped), ensure_ascii=False)
        except Exception:
            return text
    return text


def _is_synthetic_user_content(text: Any) -> bool:
    value = str(text or "").lstrip()
    synthetic_prefixes = (
        "[kanban]",
        "[KANBAN TASK",
        "[ASYNC ",
        "[IMPORTANT:",
        "[Session was just handed off",
        "[Your active task list was preserved across context compression]",
    )
    return any(value.startswith(prefix) for prefix in synthetic_prefixes)


def _sqlite_ro(path: Path) -> sqlite3.Connection:
    uri = "file:" + pathname2url(str(path.resolve())) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _profile_root_from_home(root: Path) -> Path:
    return root / "profiles"


def discover_profiles(root: Path, requested: list[str] | None = None) -> list[str]:
    if requested:
        return [name for name in requested if name and name.lower() != "default"]
    profiles_root = _profile_root_from_home(root)
    if not profiles_root.is_dir():
        return []
    names = []
    for entry in sorted(profiles_root.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir() and (entry / "config.yaml").is_file():
            names.append(entry.name)
    return names


def _load_state_db_session(profile_home: Path, session_id: str, limit: int) -> dict[str, Any]:
    db_path = profile_home / "state.db"
    if not db_path.exists():
        return {"available": False, "error": "state.db missing"}

    try:
        conn = _sqlite_ro(db_path)
    except sqlite3.Error as exc:
        return {"available": False, "error": f"state.db open failed: {exc}"}

    try:
        session_row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session_row is None:
            return {"available": True, "found": False, "messages": []}

        last_message = conn.execute(
            "SELECT id, role, content, tool_name, timestamp FROM messages "
            "WHERE session_id = ? AND active = 1 ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()

        user_rows = conn.execute(
            "SELECT id, content, timestamp FROM messages "
            "WHERE session_id = ? AND role = 'user' AND active = 1 "
            "ORDER BY id DESC LIMIT 20",
            (session_id,),
        ).fetchall()
        latest_user_raw = user_rows[0] if user_rows else None
        latest_user = None
        for row in user_rows:
            if not _is_synthetic_user_content(_decode_content(row["content"])):
                latest_user = row
                break
        if latest_user is None:
            latest_user = latest_user_raw
        latest_user_ts = float(latest_user["timestamp"]) if latest_user else None

        assistant_after_user = None
        tool_after_user_count = 0
        if latest_user_ts is not None:
            assistant_after_user = conn.execute(
                "SELECT id, content, timestamp FROM messages "
                "WHERE session_id = ? AND role = 'assistant' AND active = 1 "
                "AND timestamp >= ? ORDER BY id DESC LIMIT 1",
                (session_id, latest_user_ts),
            ).fetchone()
            tool_after_user_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM messages "
                    "WHERE session_id = ? AND role = 'tool' AND active = 1 "
                    "AND timestamp >= ?",
                    (session_id, latest_user_ts),
                ).fetchone()["n"]
            )

        rows = conn.execute(
            "SELECT id, role, content, tool_name, timestamp, active, compacted "
            "FROM messages WHERE session_id = ? AND active = 1 "
            "ORDER BY id DESC LIMIT ?",
            (session_id, max(1, int(limit))),
        ).fetchall()
        rows = list(reversed(rows))
        messages = [
            {
                "id": int(row["id"]),
                "role": row["role"],
                "tool_name": row["tool_name"],
                "timestamp": _epoch_to_iso(row["timestamp"]),
                "snippet": _redact(_decode_content(row["content"]), limit=360),
            }
            for row in rows
        ]
        return {
            "available": True,
            "found": True,
            "session": {
                key: session_row[key]
                for key in (
                    "id",
                    "source",
                    "model",
                    "started_at",
                    "ended_at",
                    "end_reason",
                    "message_count",
                    "tool_call_count",
                    "api_call_count",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "estimated_cost_usd",
                    "cost_status",
                    "title",
                )
                if key in session_row.keys()
            },
            "latest_user": {
                "id": int(latest_user["id"]),
                "timestamp": _epoch_to_iso(latest_user["timestamp"]),
                "snippet": _redact(_decode_content(latest_user["content"]), limit=360),
            }
            if latest_user
            else None,
            "latest_user_raw_is_synthetic": bool(
                latest_user_raw
                and latest_user
                and int(latest_user_raw["id"]) != int(latest_user["id"])
            ),
            "assistant_after_latest_user": {
                "id": int(assistant_after_user["id"]),
                "timestamp": _epoch_to_iso(assistant_after_user["timestamp"]),
                "snippet": _redact(
                    _decode_content(assistant_after_user["content"]), limit=360
                ),
            }
            if assistant_after_user
            else None,
            "tool_messages_after_latest_user": tool_after_user_count,
            "last_message": {
                "id": int(last_message["id"]),
                "role": last_message["role"],
                "tool_name": last_message["tool_name"],
                "timestamp": _epoch_to_iso(last_message["timestamp"]),
                "snippet": _redact(_decode_content(last_message["content"]), limit=360),
            }
            if last_message
            else None,
            "messages": messages,
        }
    except sqlite3.Error as exc:
        return {"available": True, "found": False, "error": str(exc), "messages": []}
    finally:
        conn.close()


def _load_background_sessions(
    profile_home: Path, *, since_epoch: float, limit: int = 12
) -> dict[str, Any]:
    db_path = profile_home / "state.db"
    if not db_path.exists():
        return {"available": False, "sessions": [], "active_count": 0}
    try:
        conn = _sqlite_ro(db_path)
    except sqlite3.Error as exc:
        return {"available": False, "error": f"state.db open failed: {exc}", "sessions": [], "active_count": 0}

    try:
        rows = conn.execute(
            "SELECT id, source, model, started_at, ended_at, end_reason, "
            "message_count, tool_call_count, api_call_count, input_tokens, output_tokens "
            "FROM sessions WHERE source = 'subagent' "
            "AND (ended_at IS NULL OR ended_at >= ? OR started_at >= ?) "
            "ORDER BY COALESCE(ended_at, started_at, 0) DESC LIMIT ?",
            (since_epoch, since_epoch, max(1, int(limit))),
        ).fetchall()
        sessions = []
        active_count = 0
        for row in rows:
            last_message = conn.execute(
                "SELECT id, role, tool_name, timestamp, content FROM messages "
                "WHERE session_id = ? AND active = 1 ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            last_message_payload = None
            last_message_epoch = 0.0
            if last_message:
                last_message_epoch = float(last_message["timestamp"] or 0.0)
                last_message_payload = {
                    "id": int(last_message["id"]),
                    "role": last_message["role"],
                    "tool_name": last_message["tool_name"],
                    "timestamp": _epoch_to_iso(last_message["timestamp"]),
                    "snippet": _redact(_decode_content(last_message["content"]), limit=240),
                }
            is_active = row["ended_at"] is None and (
                last_message_epoch >= since_epoch or float(row["started_at"] or 0.0) >= since_epoch
            )
            if is_active:
                active_count += 1
            sessions.append(
                {
                    "id": row["id"],
                    "source": row["source"],
                    "model": row["model"],
                    "started_at": _epoch_to_iso(row["started_at"]),
                    "ended_at": _epoch_to_iso(row["ended_at"]),
                    "end_reason": row["end_reason"],
                    "message_count": row["message_count"],
                    "tool_call_count": row["tool_call_count"],
                    "api_call_count": row["api_call_count"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "active": is_active,
                    "last_message": last_message_payload,
                }
            )
        return {
            "available": True,
            "sessions": sessions,
            "active_count": active_count,
        }
    except sqlite3.Error as exc:
        return {"available": True, "error": str(exc), "sessions": [], "active_count": 0}
    finally:
        conn.close()


def _classify_log_line(line: str) -> dict[str, Any] | None:
    event: dict[str, Any] | None = None
    if "inbound message:" in line:
        event = {"kind": "inbound", "line": _redact(line, limit=700)}
        match = _INBOUND_RE.search(line)
        if match:
            event["message_preview"] = _redact(match.group("msg"), limit=240)
    elif "response ready:" in line:
        event = {"kind": "response_ready", "line": _redact(line, limit=700)}
        match = _RESPONSE_RE.search(line)
        if match:
            event.update(
                {
                    "seconds": float(match.group("seconds")),
                    "api_calls": int(match.group("api_calls")),
                    "response_chars": int(match.group("chars")),
                }
            )
    elif "Session hygiene:" in line:
        event = {"kind": "compression", "line": _redact(line, limit=700)}
    elif "WARNING" in line:
        event = {"kind": "warning", "line": _redact(line, limit=700)}
    elif "ERROR" in line or "Traceback" in line:
        event = {"kind": "error", "line": _redact(line, limit=700)}
    elif "[kanban]" in line:
        event = {"kind": "kanban", "line": _redact(line, limit=700)}
        match = _KANBAN_EVENT_RE.search(line)
        if match:
            event["task_id"] = match.group("task_id")
            event["task_state"] = match.group("state")
    elif _ASYNC_BATCH_RE.search(line):
        event = {"kind": "async_delegation_complete", "line": _redact(line, limit=700)}

    if event is None:
        return None
    epoch = _parse_log_epoch(line)
    if epoch is not None:
        event["timestamp"] = _epoch_to_iso(epoch)
        event["_epoch"] = epoch
    return event


def _load_log_events(profile_home: Path, *, lines: int, since_epoch: float) -> list[dict[str, Any]]:
    log_path = profile_home / "logs" / "gateway.log"
    if not log_path.exists():
        return []
    try:
        raw_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events = []
    for line in raw_lines[-max(1, int(lines)) :]:
        event = _classify_log_line(line)
        if not event:
            continue
        epoch = event.get("_epoch")
        if isinstance(epoch, (int, float)) and epoch < since_epoch:
            continue
        event.pop("_epoch", None)
        events.append(event)
    return events


def _route_sessions(profile_home: Path, message_limit: int) -> list[dict[str, Any]]:
    sessions_path = profile_home / "sessions" / "sessions.json"
    data = _read_json(sessions_path)
    routes = []
    for key, value in data.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        session_id = str(value.get("session_id") or "")
        route = {
            "session_key": key,
            "session_id": session_id,
            "updated_at": value.get("updated_at"),
            "platform": value.get("platform"),
            "chat_type": value.get("chat_type"),
            "display_name": _redact(value.get("display_name"), limit=120),
            "last_prompt_tokens": value.get("last_prompt_tokens"),
            "suspended": bool(value.get("suspended")),
            "resume_pending": bool(value.get("resume_pending")),
        }
        if session_id:
            route["state_db"] = _load_state_db_session(
                profile_home, session_id, message_limit
            )
        routes.append(route)
    return sorted(routes, key=_route_priority, reverse=True)


def _is_synthetic_kanban_user(latest_user: Any) -> bool:
    if not isinstance(latest_user, dict):
        return False
    return _is_synthetic_user_content(latest_user.get("snippet"))


def _route_priority(route: dict[str, Any]) -> tuple[int, int, int, int, int, int, float, float]:
    db = route.get("state_db") if isinstance(route.get("state_db"), dict) else {}
    session = db.get("session") if isinstance(db, dict) else {}
    latest_user = db.get("latest_user") if isinstance(db, dict) else None
    assistant_after_user = (
        db.get("assistant_after_latest_user") if isinstance(db, dict) else None
    )
    tool_after_user = int(db.get("tool_messages_after_latest_user") or 0) if db else 0
    last_message = db.get("last_message") if isinstance(db, dict) else None
    has_progress = bool(assistant_after_user) or tool_after_user > 0
    human_latest = bool(latest_user) and not _is_synthetic_kanban_user(latest_user)
    last_activity_epoch = max(
        _parse_iso_epoch((last_message or {}).get("timestamp")) if isinstance(last_message, dict) else 0.0,
        _parse_iso_epoch((assistant_after_user or {}).get("timestamp"))
        if isinstance(assistant_after_user, dict)
        else 0.0,
        _parse_iso_epoch((latest_user or {}).get("timestamp"))
        if isinstance(latest_user, dict)
        else 0.0,
        _parse_iso_epoch(route.get("updated_at")),
    )
    route_updated_epoch = _parse_iso_epoch(route.get("updated_at"))
    ended_at = (session or {}).get("ended_at") if isinstance(session, dict) else None
    return (
        1 if db.get("found") else 0,
        1 if not ended_at else 0,
        1 if human_latest and has_progress else 0,
        1 if has_progress else 0,
        1 if human_latest else 0,
        1 if route.get("chat_type") == "dm" else 0,
        last_activity_epoch,
        route_updated_epoch,
    )


def _score_profile(
    gateway_state: dict[str, Any],
    routes: list[dict[str, Any]],
    events: list[dict[str, Any]],
    background: dict[str, Any] | None = None,
) -> dict[str, Any]:
    telegram = ((gateway_state.get("platforms") or {}).get("telegram") or {}).get("state")
    running = gateway_state.get("gateway_state") == "running"
    connected = telegram == "connected"
    active_agents = int(gateway_state.get("active_agents") or 0)
    counts = Counter(event.get("kind") for event in events)
    recent_errors = counts.get("error", 0)
    recent_warnings = counts.get("warning", 0)
    active_background = int((background or {}).get("active_count") or 0)

    primary = routes[0] if routes else {}
    db = primary.get("state_db") if isinstance(primary.get("state_db"), dict) else {}
    latest_user = db.get("latest_user") if isinstance(db, dict) else None
    assistant_after_user = (
        db.get("assistant_after_latest_user") if isinstance(db, dict) else None
    )
    tool_after_user = int(db.get("tool_messages_after_latest_user") or 0) if db else 0

    if not running or not connected:
        state = "offline"
    elif active_agents > 0 and assistant_after_user:
        state = "active_with_progress"
    elif active_agents > 0 and tool_after_user > 0:
        state = "active_with_tool_progress"
    elif active_agents > 0:
        state = "active_no_persisted_progress_yet"
    elif active_background > 0:
        state = "background_subagents_active"
    elif latest_user and assistant_after_user:
        state = "idle_after_response"
    elif latest_user:
        state = "idle_without_response_after_latest_user"
    else:
        state = "idle_no_recent_session"

    score = 0.0
    score += 0.25 if running else 0.0
    score += 0.20 if connected else 0.0
    score += 0.15 if routes else 0.0
    if assistant_after_user:
        score += 0.30
    elif tool_after_user > 0:
        score += 0.22
    elif active_agents > 0:
        score += 0.15
    elif active_background > 0:
        score += 0.15
    score -= min(0.25, recent_errors * 0.10 + recent_warnings * 0.03)
    score = max(0.0, min(1.0, score))

    needs_attention = (
        state in {"offline", "idle_without_response_after_latest_user"}
        or recent_errors > 0
        or (recent_warnings > 0 and active_agents == 0)
    )
    return {
        "state": state,
        "score": round(score, 2),
        "needs_attention": needs_attention,
        "signals": {
            "gateway_running": running,
            "telegram_connected": connected,
            "active_agents": active_agents,
            "active_background_sessions": active_background,
            "routes": len(routes),
            "latest_user": bool(latest_user),
            "assistant_after_latest_user": bool(assistant_after_user),
            "tool_messages_after_latest_user": tool_after_user,
            "recent_events": dict(counts),
        },
    }


def collect_profile_trace(
    root: Path,
    profile: str,
    *,
    since_epoch: float,
    log_lines: int,
    message_limit: int,
) -> dict[str, Any]:
    profile_home = root / "profiles" / profile
    gateway_state = _read_json(profile_home / "gateway_state.json")
    events = _load_log_events(profile_home, lines=log_lines, since_epoch=since_epoch)
    routes = _route_sessions(profile_home, message_limit=message_limit)
    background = _load_background_sessions(profile_home, since_epoch=since_epoch)
    evaluation = _score_profile(gateway_state, routes, events, background)
    return {
        "profile": profile,
        "home": str(profile_home),
        "gateway": {
            "pid": gateway_state.get("pid"),
            "gateway_state": gateway_state.get("gateway_state"),
            "active_agents": gateway_state.get("active_agents", 0),
            "updated_at": gateway_state.get("updated_at"),
            "telegram_state": (
                ((gateway_state.get("platforms") or {}).get("telegram") or {}).get("state")
            ),
        },
        "evaluation": evaluation,
        "routes": routes,
        "background_sessions": background,
        "recent_events": events,
    }


def _kanban_db_paths(root: Path) -> list[tuple[str, Path]]:
    paths = [("default", root / "kanban.db")]
    boards_root = root / "kanban" / "boards"
    if boards_root.is_dir():
        for child in sorted(boards_root.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir():
                paths.append((child.name, child / "kanban.db"))
    return paths


def collect_kanban_trace(root: Path) -> dict[str, Any]:
    active_board = "default"
    current_path = root / "kanban" / "current"
    try:
        if current_path.exists():
            active_board = current_path.read_text(encoding="utf-8").strip() or "default"
    except OSError:
        pass

    boards = []
    for slug, db_path in _kanban_db_paths(root):
        if not db_path.exists():
            boards.append({"slug": slug, "exists": False})
            continue
        try:
            conn = _sqlite_ro(db_path)
        except sqlite3.Error as exc:
            boards.append({"slug": slug, "exists": True, "error": str(exc)})
            continue
        try:
            counts = {
                row["status"]: int(row["n"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks "
                    "WHERE status != 'archived' GROUP BY status"
                ).fetchall()
            }
            assignees: dict[str, dict[str, int]] = {}
            for row in conn.execute(
                "SELECT assignee, status, COUNT(*) AS n FROM tasks "
                "WHERE status != 'archived' AND assignee IS NOT NULL "
                "GROUP BY assignee, status"
            ).fetchall():
                assignees.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])
            active_rows = conn.execute(
                "SELECT id, title, assignee, status, session_id, current_run_id, "
                "worker_pid, last_heartbeat_at, started_at "
                "FROM tasks WHERE status IN ('running', 'ready', 'blocked', 'review') "
                "ORDER BY COALESCE(started_at, last_heartbeat_at, 0) DESC LIMIT 40"
            ).fetchall()
            active_tasks = [
                {
                    "id": row["id"],
                    "title": _redact(row["title"], limit=180),
                    "assignee": row["assignee"],
                    "status": row["status"],
                    "session_id": row["session_id"],
                    "current_run_id": row["current_run_id"],
                    "worker_pid": row["worker_pid"],
                    "last_heartbeat_at": _epoch_to_iso(row["last_heartbeat_at"]),
                    "started_at": _epoch_to_iso(row["started_at"]),
                }
                for row in active_rows
            ]
            boards.append(
                {
                    "slug": slug,
                    "exists": True,
                    "active": slug == active_board,
                    "counts": counts,
                    "by_assignee": assignees,
                    "active_tasks": active_tasks,
                }
            )
        except sqlite3.Error as exc:
            boards.append({"slug": slug, "exists": True, "error": str(exc)})
        finally:
            conn.close()
    return {"active_board": active_board, "boards": boards}


def collect_trace(args: Any, *, root: Path | None = None) -> dict[str, Any]:
    root = root or get_default_hermes_root()
    since_minutes = max(1, int(getattr(args, "since_minutes", 120) or 120))
    since_epoch = time.time() - since_minutes * 60
    profiles = discover_profiles(root, getattr(args, "profiles", None))
    profile_traces = [
        collect_profile_trace(
            root,
            profile,
            since_epoch=since_epoch,
            log_lines=max(1, int(getattr(args, "log_lines", 500) or 500)),
            message_limit=max(1, int(getattr(args, "message_limit", 24) or 24)),
        )
        for profile in profiles
    ]
    counts = Counter(p["evaluation"]["state"] for p in profile_traces)
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "root": str(root),
        "window": {
            "since_minutes": since_minutes,
            "since": _epoch_to_iso(since_epoch),
        },
        "summary": {
            "profiles": len(profile_traces),
            "active_profiles": sum(
                1 for p in profile_traces if p["gateway"].get("active_agents", 0)
            ),
            "active_background_profiles": sum(
                1
                for p in profile_traces
                if (p.get("background_sessions") or {}).get("active_count", 0)
            ),
            "needs_attention": sum(
                1 for p in profile_traces if p["evaluation"].get("needs_attention")
            ),
            "states": dict(counts),
        },
        "profiles": profile_traces,
        "kanban": collect_kanban_trace(root),
    }


def render_markdown(trace: dict[str, Any]) -> str:
    lines = [
        "# Hermes Eval Trace",
        "",
        f"- Generated: {trace.get('generated_at')}",
        f"- Root: `{trace.get('root')}`",
        f"- Window: last {trace.get('window', {}).get('since_minutes')} minutes",
        "",
        "## Profile Scorecard",
        "",
        "| Profile | State | Score | Active | Background | Telegram | Needs attention | Latest user |",
        "| --- | --- | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for profile in trace.get("profiles", []):
        route = (profile.get("routes") or [{}])[0]
        db = route.get("state_db") if isinstance(route.get("state_db"), dict) else {}
        latest_user = db.get("latest_user") if isinstance(db, dict) else None
        latest_user_text = _redact(
            latest_user.get("snippet") if isinstance(latest_user, dict) else "",
            limit=90,
        )
        lines.append(
            "| {profile} | {state} | {score:.2f} | {active} | {background} | {telegram} | {attention} | {latest} |".format(
                profile=profile.get("profile"),
                state=profile.get("evaluation", {}).get("state"),
                score=float(profile.get("evaluation", {}).get("score") or 0),
                active=profile.get("gateway", {}).get("active_agents", 0),
                background=(profile.get("background_sessions") or {}).get(
                    "active_count", 0
                ),
                telegram=profile.get("gateway", {}).get("telegram_state") or "-",
                attention="yes"
                if profile.get("evaluation", {}).get("needs_attention")
                else "no",
                latest=latest_user_text.replace("|", "\\|") or "-",
            )
        )
    lines.extend(["", "## Kanban", ""])
    for board in trace.get("kanban", {}).get("boards", []):
        marker = " (active)" if board.get("active") else ""
        if not board.get("exists"):
            lines.append(f"- `{board.get('slug')}`{marker}: missing")
            continue
        if board.get("error"):
            lines.append(f"- `{board.get('slug')}`{marker}: error: {board.get('error')}")
            continue
        counts = ", ".join(
            f"{k}={v}" for k, v in sorted((board.get("counts") or {}).items())
        )
        lines.append(f"- `{board.get('slug')}`{marker}: {counts or 'empty'}")
        for task in board.get("active_tasks", [])[:10]:
            lines.append(
                f"  - {task.get('status')} {task.get('id')} @{task.get('assignee') or '-'}: "
                f"{task.get('title')}"
            )
    lines.extend(["", "## Recent Warnings And Errors", ""])
    any_issue = False
    for profile in trace.get("profiles", []):
        issues = [
            event
            for event in profile.get("recent_events", [])
            if event.get("kind") in {"warning", "error"}
        ]
        if not issues:
            continue
        any_issue = True
        lines.append(f"### {profile.get('profile')}")
        for event in issues[-8:]:
            lines.append(f"- `{event.get('kind')}` {event.get('timestamp') or ''}: {event.get('line')}")
    if not any_issue:
        lines.append("No warning/error events found in the selected window.")
    lines.append("")
    return "\n".join(lines)


def _artifact_paths(output_dir: Path) -> tuple[Path, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return (
        output_dir / f"hermes-eval-trace-{stamp}.json",
        output_dir / f"hermes-eval-trace-{stamp}.md",
    )


def _write_artifacts(trace: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = _artifact_paths(output_dir)
    json_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(trace), encoding="utf-8")
    return json_path, md_path


def render_summary(trace: dict[str, Any], *, artifact_paths: tuple[Path, Path] | None) -> str:
    lines = [
        "Hermes eval trace",
        f"Generated: {trace.get('generated_at')}",
        f"Profiles: {trace.get('summary', {}).get('profiles')} "
        f"(active={trace.get('summary', {}).get('active_profiles')}, "
        f"background={trace.get('summary', {}).get('active_background_profiles')}, "
        f"needs_attention={trace.get('summary', {}).get('needs_attention')})",
        "",
    ]
    for profile in trace.get("profiles", []):
        evaluation = profile.get("evaluation") or {}
        gateway = profile.get("gateway") or {}
        route = (profile.get("routes") or [{}])[0]
        db = route.get("state_db") if isinstance(route.get("state_db"), dict) else {}
        latest_user = db.get("latest_user") if isinstance(db, dict) else None
        latest_user_text = _redact(
            latest_user.get("snippet") if isinstance(latest_user, dict) else "",
            limit=100,
        )
        active_background = (profile.get("background_sessions") or {}).get(
            "active_count", 0
        )
        lines.append(
            "- {profile}: {state} score={score:.2f} active={active} background={background} telegram={telegram} latest_user={latest}".format(
                profile=profile.get("profile"),
                state=evaluation.get("state"),
                score=float(evaluation.get("score") or 0),
                active=gateway.get("active_agents", 0),
                background=active_background,
                telegram=gateway.get("telegram_state") or "-",
                latest=latest_user_text or "-",
            )
        )
    if artifact_paths:
        lines.extend(
            [
                "",
                f"JSON: {artifact_paths[0]}",
                f"Markdown: {artifact_paths[1]}",
            ]
        )
    return "\n".join(lines)


def cmd_trace(args: Any) -> int:
    root = get_default_hermes_root()
    trace = collect_trace(args, root=root)
    artifact_paths = None
    if not getattr(args, "no_write", False):
        output_dir = (
            Path(getattr(args, "output_dir")).expanduser()
            if getattr(args, "output_dir", None)
            else root / "eval_traces"
        )
        artifact_paths = _write_artifacts(trace, output_dir)
    if getattr(args, "json", False):
        print(json.dumps(trace, indent=2, ensure_ascii=False))
    else:
        print(render_summary(trace, artifact_paths=artifact_paths))
    return 0
