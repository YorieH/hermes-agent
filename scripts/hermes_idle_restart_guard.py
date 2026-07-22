"""Fail-closed probe and owned drain controls for the Hermes idle restarter."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

import psutil
import yaml

from gateway.drain_control import (
    clear_drain_request,
    read_drain_request,
    write_drain_request,
)
from gateway.status import get_process_start_time


def _strict_int(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"gateway {name} must be a JSON integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise RuntimeError(f"gateway {name} must be {qualifier}")
    return value


def _positive_process_id(value: str) -> int:
    try:
        process_id = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("owner PID must be an integer") from exc
    if process_id <= 0:
        raise argparse.ArgumentTypeError("owner PID must be positive")
    return process_id


def _positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("lease seconds must be an integer") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("lease seconds must be positive")
    return seconds


def _runtime_executable_matches(expected: Path, live: Path) -> bool:
    """Accept only the selected venv or its exact base-runtime executables.

    Windows launcher-style virtual environments report the base interpreter
    through ``psutil.Process.exe()``, while copied virtual environments can
    report the executable under ``venv\Scripts``. Gateways may use either
    ``python.exe`` or ``pythonw.exe``. Enumerating those exact files supports
    both layouts without accepting an arbitrary interpreter in the same tree.
    """
    expected = expected.resolve(strict=True)
    live = live.resolve(strict=True)
    roots = {expected.parent, Path(sys.base_prefix).resolve(strict=True)}
    allowed: set[Path] = set()
    for root in roots:
        for name in ("python.exe", "pythonw.exe"):
            candidate = root / name
            if candidate.is_file():
                allowed.add(candidate.resolve(strict=True))
    return live in allowed


def _profile_gateway_processes(profile: str) -> list[psutil.Process]:
    matches: list[psutil.Process] = []
    for process in psutil.process_iter(("pid", "exe", "cmdline", "status")):
        try:
            if process.info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            command = [str(item) for item in (process.info.get("cmdline") or [])]
            if len(command) < 7:
                continue
            module_ok = any(
                command[index] == "-m" and command[index + 1] == "hermes_cli.main"
                for index in range(len(command) - 1)
            )
            profile_ok = any(
                command[index] == "--profile" and command[index + 1] == profile
                for index in range(len(command) - 1)
            )
            gateway_run = any(
                command[index] == "gateway" and command[index + 1] == "run"
                for index in range(len(command) - 1)
            )
            executable = Path(str(process.info.get("exe") or ""))
            command_executable = Path(command[0])
            executable_ok = (
                executable.is_file()
                and executable.name.lower() in {"python.exe", "pythonw.exe"}
                and command_executable.resolve() == executable.resolve()
            )
            if module_ok and profile_ok and gateway_run and executable_ok:
                matches.append(process)
        except (psutil.Error, OSError, RuntimeError):
            continue
    return matches


def _profile_default_assignee(profile_home: Path) -> str:
    """Read the dispatcher fallback that owns otherwise-unassigned work."""
    config_path = profile_home / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"cannot read profile kanban config: {config_path}") from exc
    if not isinstance(config, dict):
        raise RuntimeError(f"profile config must be a YAML mapping: {config_path}")
    kanban = config.get("kanban") or {}
    if not isinstance(kanban, dict):
        raise RuntimeError("profile kanban config must be a mapping")
    raw = kanban.get("default_assignee") or ""
    if not isinstance(raw, str):
        raise RuntimeError("kanban.default_assignee must be a string")
    return raw.strip().lstrip("@").lower()


def _kanban_busy_count(
    root: Path,
    profile: str,
    *,
    default_assignee: str = "",
) -> tuple[int, int]:
    primary = root / "kanban.db"
    if not primary.is_file() or primary.stat().st_size <= 0:
        raise RuntimeError(f"required kanban database is missing or empty: {primary}")

    boards_root = root / "kanban" / "boards"
    if boards_root.exists():
        for board_manifest in boards_root.glob("*/board.json"):
            expected = board_manifest.parent / "kanban.db"
            if not expected.is_file() or expected.stat().st_size <= 0:
                raise RuntimeError(
                    f"named board has no readable non-empty kanban database: {expected}"
                )

    candidates = [primary]
    if boards_root.exists():
        candidates.extend(sorted(boards_root.glob("*/kanban.db")))

    seen_paths: set[Path] = set()
    seen_files: set[tuple[int, int]] = set()
    busy = 0
    checked = 0
    for path in candidates:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
        file_identity = (int(stat.st_dev), int(stat.st_ino))
        if resolved in seen_paths or file_identity in seen_files:
            raise RuntimeError(f"kanban database is exposed by more than one path: {path}")
        seen_paths.add(resolved)
        seen_files.add(file_identity)
        connection = sqlite3.connect(
            f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=2.0
        )
        try:
            check = connection.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                raise RuntimeError(f"kanban database quick_check failed: {path}")
            rows = connection.execute(
                "SELECT status, assignee FROM tasks WHERE status IN ('running', 'ready')"
            ).fetchall()
        finally:
            connection.close()
        checked += 1
        for status, assignee in rows:
            normalized = str(assignee or "").strip().lstrip("@").lower()
            if status == "running" and normalized == profile:
                busy += 1
            elif status == "ready":
                # Unassigned Ready work belongs only to the profile this
                # gateway would route through kanban.default_assignee. Without
                # a configured fallback it is not evidence that every profile
                # is busy. Explicit assignments remain conservative.
                routed = (
                    default_assignee
                    if normalized in {"", "unassigned"}
                    else normalized
                )
                if routed not in {"", "unassigned"} and routed == profile:
                    busy += 1
    return busy, checked


def probe(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve(strict=True)
    profile_home = (root / "profiles" / args.profile).resolve(strict=True)
    state_path = profile_home / "gateway_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise RuntimeError("gateway state must be a JSON object")
    required = {
        "pid", "start_time", "kind", "gateway_state", "restart_requested",
        "active_agents", "updated_at", "platforms",
    }
    missing = sorted(required - set(state))
    if missing:
        raise RuntimeError("gateway state is missing required fields: " + ", ".join(missing))

    pid = _strict_int(state["pid"], "pid", positive=True)
    start_time = _strict_int(state["start_time"], "start_time", positive=True)
    active_agents = _strict_int(state["active_agents"], "active_agents")
    if state["kind"] != "hermes-gateway":
        raise RuntimeError("gateway kind is not hermes-gateway")
    if not isinstance(state["restart_requested"], bool):
        raise RuntimeError("gateway restart_requested must be a JSON boolean")
    if state["restart_requested"]:
        raise RuntimeError("gateway already has a restart request in progress")
    if state["gateway_state"] not in set(args.allowed_state):
        raise RuntimeError(
            f"gateway state {state['gateway_state']!r} is outside allowed states"
        )

    updated = datetime.fromisoformat(str(state["updated_at"]).replace("Z", "+00:00"))
    if updated.tzinfo is None:
        raise RuntimeError("gateway updated_at must include a timezone")
    age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
    if age < -5 or age > args.maximum_state_age:
        raise RuntimeError(f"gateway state is stale or future-dated: age_seconds={age:.1f}")

    processes = _profile_gateway_processes(args.profile)
    if len(processes) != 1:
        raise RuntimeError(
            f"expected exactly one live {args.profile} gateway, found {len(processes)}"
        )
    process = processes[0]
    expected_executable = args.expected_executable.resolve(strict=True)
    live_executable = Path(process.exe()).resolve(strict=True)
    python_names = {"python.exe", "pythonw.exe"}
    if (
        expected_executable != Path(sys.executable).resolve(strict=True)
        or expected_executable.name.lower() not in python_names
        or live_executable.name.lower() not in python_names
        or not _runtime_executable_matches(expected_executable, live_executable)
    ):
        raise RuntimeError("gateway executable does not match the requested Hermes runtime")
    if process.pid != pid:
        raise RuntimeError("gateway state PID does not match the unique profile gateway")
    live_start = get_process_start_time(pid)
    if live_start is None or live_start != start_time:
        raise RuntimeError("gateway PID start-time identity does not match live process")
    if args.expected_pid is not None and pid != args.expected_pid:
        raise RuntimeError("gateway PID changed during the idle observation")
    if args.expected_start_time is not None and start_time != args.expected_start_time:
        raise RuntimeError("gateway start-time changed during the idle observation")

    platforms = state["platforms"]
    if not isinstance(platforms, dict):
        raise RuntimeError("gateway platforms must be a JSON object")
    telegram = platforms.get("telegram")
    telegram_state = telegram.get("state") if isinstance(telegram, dict) else None
    if args.require_telegram_connected and telegram_state != "connected":
        raise RuntimeError("Telegram platform is not connected")

    default_assignee = _profile_default_assignee(profile_home)
    kanban_busy, databases_checked = _kanban_busy_count(
        root,
        args.profile,
        default_assignee=default_assignee,
    )
    return {
        "pid": pid,
        "start_time": start_time,
        "active_agents": active_agents,
        "gateway_state": state["gateway_state"],
        "telegram_state": telegram_state,
        "kanban_busy": kanban_busy,
        "databases_checked": databases_checked,
        "updated_at": updated.astimezone(timezone.utc).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("--root", type=Path, required=True)
    probe_parser.add_argument("--profile", required=True)
    probe_parser.add_argument("--maximum-state-age", type=int, required=True)
    probe_parser.add_argument("--expected-executable", type=Path, required=True)
    probe_parser.add_argument("--allowed-state", action="append", required=True)
    probe_parser.add_argument("--expected-pid", type=int)
    probe_parser.add_argument("--expected-start-time", type=int)
    probe_parser.add_argument("--require-telegram-connected", action="store_true")

    begin = subparsers.add_parser("drain-begin")
    begin.add_argument("--home", type=Path, required=True)
    begin.add_argument("--principal", required=True)
    begin.add_argument("--owner-pid", type=_positive_process_id, required=True)
    begin.add_argument("--lease-seconds", type=_positive_seconds, required=True)
    clear = subparsers.add_parser("drain-clear")
    clear.add_argument("--home", type=Path, required=True)
    clear.add_argument("--principal", required=True)
    status = subparsers.add_parser("drain-status")
    status.add_argument("--home", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "probe":
            result = probe(args)
        elif args.command == "drain-begin":
            result = write_drain_request(
                home=args.home.resolve(strict=True),
                principal=args.principal,
                require_absent=True,
                lease_seconds=args.lease_seconds,
                owner_pid=args.owner_pid,
            )
        elif args.command == "drain-clear":
            removed = clear_drain_request(
                home=args.home.resolve(strict=True),
                expected_principal=args.principal,
            )
            result = {"removed": removed, "remaining": read_drain_request(home=args.home)}
            if not removed:
                raise RuntimeError("owned drain marker was not removed")
        else:
            result = {"marker": read_drain_request(home=args.home.resolve(strict=True))}
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
