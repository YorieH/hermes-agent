"""Behavioral coverage for the Windows Hermes idle-restart helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from gateway.drain_control import drain_requested, read_drain_request


ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "hermes_idle_restart_guard.py"
RESTART_HELPER = ROOT / "scripts" / "Restart-HermesGatewayWhenIdle.ps1"


def _run_guard(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_drain_begin_binds_lease_to_explicit_live_owner(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    principal = "idle-restart:test-live-owner"

    begun = _run_guard(
        home,
        "drain-begin",
        "--home",
        str(home),
        "--principal",
        principal,
        "--owner-pid",
        str(os.getpid()),
    )

    assert begun.returncode == 0, begun.stderr
    payload = json.loads(begun.stdout)
    assert payload["owner_pid"] == os.getpid()
    assert payload["owner_start_time"] > 0
    assert payload["principal"] == principal
    assert drain_requested(home=home) is True

    cleared = _run_guard(
        home,
        "drain-clear",
        "--home",
        str(home),
        "--principal",
        principal,
    )
    assert cleared.returncode == 0, cleared.stderr
    assert read_drain_request(home=home) is None


def test_drain_begin_dead_owner_self_releases(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    impossible_windows_pid = 2_147_483_647

    begun = _run_guard(
        home,
        "drain-begin",
        "--home",
        str(home),
        "--principal",
        "idle-restart:test-dead-owner",
        "--owner-pid",
        str(impossible_windows_pid),
    )

    assert begun.returncode == 0, begun.stderr
    assert json.loads(begun.stdout)["owner_pid"] == impossible_windows_pid
    assert drain_requested(home=home) is False
    assert read_drain_request(home=home) is None


def test_drain_begin_requires_positive_explicit_owner(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()

    missing = _run_guard(
        home,
        "drain-begin",
        "--home",
        str(home),
        "--principal",
        "idle-restart:test-missing-owner",
    )
    invalid = _run_guard(
        home,
        "drain-begin",
        "--home",
        str(home),
        "--principal",
        "idle-restart:test-invalid-owner",
        "--owner-pid",
        "0",
    )

    assert missing.returncode == 2
    assert "--owner-pid" in missing.stderr
    assert invalid.returncode == 2
    assert "positive" in invalid.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell helper")
def test_dry_run_overrides_mismatched_inherited_profile_environment(tmp_path):
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")

    hermes_root = tmp_path / "Hermes Root"
    profile_home = hermes_root / "profiles" / "rikku"
    scripts = hermes_root / "scripts"
    profile_home.mkdir(parents=True)
    scripts.mkdir(parents=True)
    fake_executable = hermes_root / "fake-hermes.exe"
    fake_executable.write_bytes(b"")
    fake_guard = scripts / "hermes_idle_restart_guard.py"
    fake_guard.write_text(
        """
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
if not arguments or arguments[0] != "probe":
    raise SystemExit("unexpected command")

def value(flag):
    return arguments[arguments.index(flag) + 1]

root = Path(value("--root")).resolve()
profile = value("--profile")
expected_home = (root / "profiles" / profile).resolve()
if os.environ.get("HERMES_PROFILE") != profile:
    raise SystemExit("wrong HERMES_PROFILE")
if Path(os.environ.get("HERMES_HOME", "")).resolve() != expected_home:
    raise SystemExit("wrong HERMES_HOME")

print(json.dumps({
    "pid": 1234,
    "start_time": 5678,
    "active_agents": 0,
    "gateway_state": "running",
    "telegram_state": "connected",
    "kanban_busy": 0,
    "databases_checked": 1,
    "updated_at": "2026-07-19T00:00:00+00:00",
}))
""".lstrip(),
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment["HERMES_PROFILE"] = "kurumi"
    environment["HERMES_HOME"] = str(tmp_path / "wrong-profile-home")
    environment.pop("_HERMES_GATEWAY", None)
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RESTART_HELPER),
            "-Profile",
            "rikku",
            "-TimeoutSeconds",
            "8",
            "-PollSeconds",
            "1",
            "-MinimumIdleSamples",
            "2",
            "-DryRun",
            "-HermesRoot",
            str(hermes_root),
            "-HermesPython",
            sys.executable,
            "-HermesExecutable",
            str(fake_executable),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    log = (hermes_root / "logs" / "Restart-HermesGatewayWhenIdle-rikku.log")
    assert "dry_run_idle_confirmed profile=rikku" in log.read_text(encoding="utf-8-sig")
