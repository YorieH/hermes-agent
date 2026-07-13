"""Shared Kanban notification subscription helpers."""

from __future__ import annotations

import logging
import os
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from hermes_cli.config import cfg_get, get_hermes_home, load_config

logger = logging.getLogger(__name__)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _session_env(name: str, default: str = "") -> str:
    """Read a gateway session variable with os.environ fallback."""
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, default)
    except Exception:
        return os.environ.get(name, default)


def _copy_notify_sub(
    conn: Any,
    task_id: str,
    row: Any,
    *,
    notifier_profile: Optional[str] = None,
) -> bool:
    platform = str(_row_get(row, "platform", "") or "")
    chat_id = str(_row_get(row, "chat_id", "") or "")
    if not platform or not chat_id:
        return False

    from hermes_cli import kanban_db as kb

    owner = (
        notifier_profile
        or _row_get(row, "notifier_profile")
        or os.environ.get("HERMES_KANBAN_NOTIFIER_PROFILE")
        or os.environ.get("HERMES_PROFILE")
        or None
    )
    kb.add_notify_sub(
        conn,
        task_id=task_id,
        platform=platform,
        chat_id=chat_id,
        thread_id=_row_get(row, "thread_id") or None,
        user_id=_row_get(row, "user_id") or None,
        notifier_profile=owner,
    )
    return True


def _candidate_session_indexes() -> list[tuple[Optional[str], Path]]:
    """Return likely sessions.json files for the default and named profiles."""
    seen: set[str] = set()
    out: list[tuple[Optional[str], Path]] = []

    def add(profile: Optional[str], path: Path) -> None:
        try:
            key = str(path.expanduser().resolve())
        except Exception:
            key = str(path)
        if key in seen:
            return
        seen.add(key)
        out.append((profile, path))

    try:
        add(os.environ.get("HERMES_PROFILE") or None, get_hermes_home() / "sessions" / "sessions.json")
    except Exception:
        pass

    try:
        from hermes_cli.profiles import list_profiles

        for info in list_profiles():
            add(getattr(info, "name", None), Path(info.path) / "sessions" / "sessions.json")
    except Exception:
        try:
            root = get_hermes_home()
            add("default", root / "sessions" / "sessions.json")
            profiles = root / "profiles"
            if profiles.is_dir():
                for entry in profiles.iterdir():
                    if entry.is_dir():
                        add(entry.name, entry / "sessions" / "sessions.json")
        except Exception:
            pass
    return out


def _session_routes_for_session_id(session_id: str) -> list[dict[str, Any]]:
    """Find persisted gateway session routing by Hermes session id.

    ``sessions.json`` only stores the current route for a chat identity. Long
    Telegram runs can compact into child sessions, so kanban tasks created
    before compaction may keep an ancestor ``tasks.session_id`` that no longer
    appears directly in the routing index. Treat a current routed session as a
    match when the requested id is in its ``parent_session_id`` lineage.
    """
    if not session_id:
        return []
    matches: list[dict[str, Any]] = []
    for profile, path in _candidate_session_indexes():
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            routed_session_id = str(entry.get("session_id") or "")
            if routed_session_id != str(session_id) and not _session_is_ancestor(
                path.parent.parent,
                ancestor_id=str(session_id),
                descendant_id=routed_session_id,
            ):
                continue
            origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
            session_key = str(entry.get("session_key") or key or "")
            platform = str(origin.get("platform") or entry.get("platform") or "")
            chat_id = str(origin.get("chat_id") or "")
            if not session_key and (not platform or not chat_id):
                continue
            matches.append({
                "profile": profile,
                "session_key": session_key,
                "platform": platform,
                "chat_id": chat_id,
                "thread_id": origin.get("thread_id"),
                "user_id": origin.get("user_id"),
            })
    return matches


def _session_is_ancestor(
    profile_root: Path,
    *,
    ancestor_id: str,
    descendant_id: str,
) -> bool:
    """Return True when ``ancestor_id`` is in ``descendant_id``'s lineage."""
    if not ancestor_id or not descendant_id:
        return False
    if ancestor_id == descendant_id:
        return True
    state_db = profile_root / "state.db"
    if not state_db.exists():
        return False
    uri = f"file:{state_db.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            row = conn.execute(
                """
                WITH RECURSIVE lineage(id, parent_session_id, depth) AS (
                    SELECT id, parent_session_id, 0
                      FROM sessions
                     WHERE id = ?
                    UNION ALL
                    SELECT parent.id, parent.parent_session_id, lineage.depth + 1
                      FROM sessions AS parent
                      JOIN lineage ON parent.id = lineage.parent_session_id
                     WHERE lineage.depth < 64
                )
                SELECT 1
                  FROM lineage
                 WHERE id = ?
                 LIMIT 1
                """,
                (descendant_id, ancestor_id),
            ).fetchone()
            return bool(row)
        finally:
            conn.close()
    except Exception:
        return False


def _subscribe_from_recorded_session(
    conn: Any,
    task_id: str,
    *,
    notifier_profile: Optional[str] = None,
) -> bool:
    """Subscribe a task to the persisted session that created it.

    Agent tool subprocesses may preserve ``tasks.session_id`` while losing the
    live gateway ContextVars and environment values. In that case, recover the
    session key from the profile session indexes and create an internal wake
    subscription. The gateway treats platform='tui' + chat_id='agent:...' as a
    synthetic turn target for the owning profile.
    """
    try:
        from hermes_cli import kanban_db as kb

        task = conn.execute(
            "SELECT session_id, created_by FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task:
            return False
        session_id = str(_row_get(task, "session_id", "") or "")
        if not session_id:
            return False
        from hermes_cli.profiles import profile_exists

        def _valid_profile(value: Any) -> Optional[str]:
            candidate = str(value or "").strip()
            if not candidate:
                return None
            try:
                return candidate if profile_exists(candidate) else None
            except Exception:
                return None

        explicit_owner = next(
            (
                owner
                for owner in (
                    _valid_profile(notifier_profile),
                    _valid_profile(os.environ.get("HERMES_KANBAN_NOTIFIER_PROFILE")),
                    _valid_profile(os.environ.get("HERMES_PROFILE")),
                )
                if owner
            ),
            None,
        )
        created_by_owner = _valid_profile(_row_get(task, "created_by"))
        wrote = False
        for route in _session_routes_for_session_id(session_id):
            route_owner = (
                explicit_owner
                or _valid_profile(route.get("profile"))
                or created_by_owner
            )
            session_key = str(route.get("session_key") or "")
            if session_key:
                kb.add_notify_sub(
                    conn,
                    task_id=task_id,
                    platform="tui",
                    chat_id=session_key,
                    thread_id=None,
                    user_id=route.get("user_id") or None,
                    notifier_profile=route_owner,
                )
                wrote = True
        return wrote
    except Exception as exc:
        logger.warning(
            "kanban notify session-id fallback failed for %s: %r", task_id, exc
        )
        return False


def _inherit_existing_subscription(
    conn: Any,
    task_id: str,
    *,
    notifier_profile: Optional[str] = None,
) -> bool:
    """Backfill notification routing from related kanban work.

    Agent-spawned workers do not always have the gateway's live Telegram
    session context in process-local storage. Prefer exact parent routing,
    then sibling routing, then another task from the same originating session
    or the parent task's originating session.
    """
    try:
        from hermes_cli import kanban_db as kb

        if kb.list_notify_subs(conn, task_id):
            return True

        queries = (
            (
                """
                SELECT s.*
                  FROM task_links l
                  JOIN kanban_notify_subs s ON s.task_id = l.parent_id
                 WHERE l.child_id = ?
                 ORDER BY s.created_at DESC
                 LIMIT 1
                """,
                (task_id,),
            ),
            (
                """
                SELECT s.*
                  FROM task_links mine
                  JOIN task_links sib
                    ON sib.parent_id = mine.parent_id
                   AND sib.child_id != mine.child_id
                  JOIN kanban_notify_subs s ON s.task_id = sib.child_id
                 WHERE mine.child_id = ?
                 ORDER BY s.created_at DESC
                 LIMIT 1
                """,
                (task_id,),
            ),
            (
                """
                SELECT s.*
                  FROM tasks child
                  JOIN tasks peer
                    ON peer.session_id = child.session_id
                   AND peer.id != child.id
                  JOIN kanban_notify_subs s ON s.task_id = peer.id
                 WHERE child.id = ?
                   AND child.session_id IS NOT NULL
                   AND child.session_id != ''
                 ORDER BY s.created_at DESC
                 LIMIT 1
                """,
                (task_id,),
            ),
            (
                """
                SELECT s.*
                  FROM task_links l
                  JOIN tasks parent ON parent.id = l.parent_id
                  JOIN tasks peer
                    ON peer.session_id = parent.session_id
                   AND peer.id != parent.id
                  JOIN kanban_notify_subs s ON s.task_id = peer.id
                 WHERE l.child_id = ?
                   AND parent.session_id IS NOT NULL
                   AND parent.session_id != ''
                 ORDER BY s.created_at DESC
                 LIMIT 1
                """,
                (task_id,),
            ),
        )
        for sql, params in queries:
            row = conn.execute(sql, params).fetchone()
            if row and _copy_notify_sub(
                conn,
                task_id,
                row,
                notifier_profile=notifier_profile,
            ):
                return True
    except Exception as exc:
        logger.warning("kanban notify inheritance failed for %s: %r", task_id, exc)
    return False


def maybe_auto_subscribe_task(
    conn: Any,
    task_id: str,
    *,
    notifier_profile: Optional[str] = None,
    allow_tui_fallback: bool = True,
) -> bool:
    """Subscribe the current persistent session to terminal task events.

    Returns True when a subscription row was written, False when there is no
    persistent delivery context, config disables the feature, or bookkeeping
    failed. This is best-effort by design: notification setup must never make
    task creation fail.
    """
    try:
        cfg = load_config()
        if not cfg_get(cfg, "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        # Preserve the default-on behavior if config loading itself is broken.
        pass

    platform = ""
    chat_id = ""
    try:
        platform = _session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = _session_env("HERMES_SESSION_CHAT_ID", "")
        if (not platform or not chat_id) and allow_tui_fallback:
            session_key = (
                _session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                return (
                    _inherit_existing_subscription(
                        conn,
                        task_id,
                        notifier_profile=notifier_profile,
                    )
                    or _subscribe_from_recorded_session(
                        conn,
                        task_id,
                        notifier_profile=notifier_profile,
                    )
                )
            platform = "tui"
            chat_id = session_key
        if not platform or not chat_id:
            return (
                _inherit_existing_subscription(
                    conn,
                    task_id,
                    notifier_profile=notifier_profile,
                )
                or _subscribe_from_recorded_session(
                    conn,
                    task_id,
                    notifier_profile=notifier_profile,
                )
            )

        thread_id = _session_env("HERMES_SESSION_THREAD_ID", "") or None
        user_id = _session_env("HERMES_SESSION_USER_ID", "") or None
        owner = (
            notifier_profile
            or os.environ.get("HERMES_KANBAN_NOTIFIER_PROFILE")
            or os.environ.get("HERMES_PROFILE")
            or None
        )

        from hermes_cli import kanban_db as kb

        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            notifier_profile=owner,
        )
        wrote = True
        if allow_tui_fallback:
            session_key = (
                _session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if session_key:
                kb.add_notify_sub(
                    conn,
                    task_id=task_id,
                    platform="tui",
                    chat_id=session_key,
                    thread_id=None,
                    user_id=user_id,
                    notifier_profile=owner,
                )
        return wrote
    except Exception as exc:
        logger.warning(
            "kanban auto-subscribe failed for %s: %r (platform=%r chat_set=%r)",
            task_id,
            exc,
            platform,
            bool(chat_id),
        )
        return False
