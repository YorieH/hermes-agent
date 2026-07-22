"""Behavioral coverage for the Windows Hermes idle-restart helpers."""
from __future__ import annotations

import importlib.util
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from gateway.drain_control import drain_requested, read_drain_request


ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "hermes_idle_restart_guard.py"
RESTART_HELPER = ROOT / "scripts" / "Restart-HermesGatewayWhenIdle.ps1"

_GUARD_SPEC = importlib.util.spec_from_file_location("hermes_idle_restart_guard", GUARD)
assert _GUARD_SPEC is not None and _GUARD_SPEC.loader is not None
guard_module = importlib.util.module_from_spec(_GUARD_SPEC)
_GUARD_SPEC.loader.exec_module(guard_module)


def test_idle_restart_defaults_cover_observed_windows_gateway_latency():
    source = RESTART_HELPER.read_text(encoding="ascii")
    assert "[int]$DrainAcknowledgeTimeoutSeconds = 120" in source
    assert "[int]$PostRestartTimeoutSeconds = 180" in source


def _run_guard(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_runtime_executable_accepts_launcher_and_copied_venv_layouts(
    tmp_path,
    monkeypatch,
):
    scripts = tmp_path / "venv" / "Scripts"
    base = tmp_path / "base-runtime"
    unrelated = tmp_path / "unrelated"
    for directory in (scripts, base, unrelated):
        directory.mkdir(parents=True)
        for name in ("python.exe", "pythonw.exe"):
            (directory / name).write_bytes(b"")

    expected = scripts / "python.exe"
    monkeypatch.setattr(guard_module.sys, "base_prefix", str(base))

    assert guard_module._runtime_executable_matches(expected, scripts / "pythonw.exe")
    assert guard_module._runtime_executable_matches(expected, base / "pythonw.exe")
    assert not guard_module._runtime_executable_matches(
        expected,
        unrelated / "pythonw.exe",
    )


def _write_minimal_kanban(root: Path, rows: list[tuple[str, str | None]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / "kanban.db") as connection:
        connection.execute("CREATE TABLE tasks (status TEXT, assignee TEXT)")
        connection.executemany(
            "INSERT INTO tasks(status, assignee) VALUES (?, ?)",
            rows,
        )


def test_unassigned_ready_card_does_not_mark_every_profile_busy(tmp_path):
    root = tmp_path / "hermes"
    _write_minimal_kanban(root, [("ready", None), ("ready", "unassigned")])

    assert guard_module._kanban_busy_count(root, "rikku") == (0, 1)
    assert guard_module._kanban_busy_count(root, "asuna") == (0, 1)
    assert guard_module._kanban_busy_count(
        root, "rikku", default_assignee="rikku",
    ) == (2, 1)


def test_assigned_ready_and_running_cards_remain_profile_scoped_busy(tmp_path):
    root = tmp_path / "hermes"
    _write_minimal_kanban(
        root,
        [
            ("ready", "@Rikku"),
            ("running", "rikku"),
            ("ready", "asuna"),
            ("running", "kurumi"),
            ("done", "rikku"),
        ],
    )

    assert guard_module._kanban_busy_count(root, "rikku") == (2, 1)
    assert guard_module._kanban_busy_count(root, "asuna") == (1, 1)
    assert guard_module._kanban_busy_count(root, "yuna") == (0, 1)


def test_named_board_assigned_ready_card_is_included(tmp_path):
    root = tmp_path / "hermes"
    _write_minimal_kanban(root, [])
    board = root / "kanban" / "boards" / "named"
    _write_minimal_kanban(board, [("ready", "rikku")])
    (board / "board.json").write_text("{}", encoding="utf-8")

    assert guard_module._kanban_busy_count(root, "rikku") == (1, 2)


def test_profile_default_assignee_is_normalized(tmp_path):
    home = tmp_path / "profiles" / "rikku"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "kanban:\n  default_assignee: ' @Rikku '\n",
        encoding="utf-8",
    )

    assert guard_module._profile_default_assignee(home) == "rikku"


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
        "--lease-seconds",
        "900",
    )

    assert begun.returncode == 0, begun.stderr
    payload = json.loads(begun.stdout)
    assert payload["owner_pid"] == os.getpid()
    assert payload["owner_start_time"] > 0
    assert payload["principal"] == principal
    requested = datetime.fromisoformat(payload["requested_at"])
    expires = datetime.fromisoformat(payload["lease_expires_at"])
    assert 899 <= (expires - requested).total_seconds() <= 901
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
        "--lease-seconds",
        "900",
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
        "--lease-seconds",
        "900",
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
        "--lease-seconds",
        "900",
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell helper")
def test_powershell_requires_drain_ack_and_bounds_native_restart(tmp_path):
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")

    hermes_root = tmp_path / "hermes"
    (hermes_root / "profiles" / "rikku").mkdir(parents=True)
    sleeper = tmp_path / "sleeper.ps1"
    sleeper.write_text("Start-Sleep -Seconds 10\n", encoding="ascii")
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        r'''
param($Helper, $Root, $Python, $Shell, $Sleeper)
. $Helper -Profile rikku -DryRun -HermesRoot $Root `
  -HermesPython $Python -HermesExecutable $Python `
  -DrainAcknowledgeTimeoutSeconds 11 `
  -RestartCommandTimeoutSeconds 31 `
  -PostRestartTimeoutSeconds 13

if ($DrainLeaseSeconds -ne 219) {
  throw "unexpected computed lease: $DrainLeaseSeconds"
}

$script:ProbeStates = New-Object 'System.Collections.Generic.Queue[string]'
foreach ($state in @('running', 'running', 'draining', 'draining')) {
  $script:ProbeStates.Enqueue($state)
}
$script:ProbeCalls = 0
function Assert-OwnedDrain {}
function Get-IdleSnapshot {
  param(
    [string[]]$AllowedStates = @('running'),
    [long]$ExpectedPid = 0,
    [long]$ExpectedStartTime = 0,
    [switch]$RequireTelegramConnected
  )
  $script:ProbeCalls += 1
  $state = $script:ProbeStates.Dequeue()
  if ($AllowedStates -notcontains $state) { throw "state $state not allowed" }
  return [pscustomobject]@{
    gateway_state = $state
    active_agents = 0
    kanban_busy = 0
  }
}
function Start-Sleep { param([int]$Seconds) }

$ack = Wait-ForExternalDrainIdle -GatewayPid 1234 -StartTime 5678
if ($ack.gateway_state -ne 'draining' -or $script:ProbeCalls -ne 4) {
  throw "running samples were incorrectly accepted"
}
Remove-Item Function:Start-Sleep

$watch = [Diagnostics.Stopwatch]::StartNew()
$timedOut = $false
try {
  $null = Invoke-TargetNativeCommandWithTimeout `
    -FilePath $Shell `
    -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Sleeper) `
    -TimeoutSeconds 1
} catch {
  if ($_.Exception.Message -like '*safety timeout*') { $timedOut = $true }
  else { throw }
}
$watch.Stop()
if (-not $timedOut -or $watch.Elapsed.TotalSeconds -gt 6) {
  throw "native restart timeout was not enforced"
}

Enter-OwnedMaintenance
$maintenanceCollisionProtected = $false
try { Enter-OwnedMaintenance }
catch { $maintenanceCollisionProtected = $true }
if (-not $maintenanceCollisionProtected) {
  throw "an existing maintenance owner was overwritten"
}

$script:LifecycleCalls = @()
function Invoke-TargetNativeCommandWithTimeout {
  param(
    [Parameter(Mandatory=$true)][string]$FilePath,
    [string[]]$Arguments = @(),
    [Parameter(Mandatory=$true)][int]$TimeoutSeconds
  )
  if ($FilePath -eq $HermesExecutable -and [string]$Arguments[-1] -eq 'stop') {
    $script:LifecycleCalls += 'stop'
  } elseif (
    $FilePath -eq $CommandShell -and
    $Arguments.Count -eq 3 -and
    $Arguments[0] -eq '/d' -and
    $Arguments[1] -eq '/c' -and
    $Arguments[2] -eq "`"$GatewayCommand`""
  ) {
    $script:LifecycleCalls += 'wrapper-start'
  } else {
    throw "unexpected lifecycle command"
  }
  return [pscustomobject]@{ Output = @(); ExitCode = 0 }
}
Invoke-GracefulProfileRestart
if (($script:LifecycleCalls -join ',') -ne 'stop,wrapper-start') {
  throw "the watchdog-safe lifecycle did not use bounded stop then detached wrapper start"
}
Assert-OwnedMaintenance
Exit-OwnedMaintenance
if (Test-Path -LiteralPath $MaintenancePath) {
  throw "the owned maintenance marker was not released"
}

[IO.File]::WriteAllText($MaintenancePath, "foreign-owner`n")
$foreignPreserved = $false
try { Exit-OwnedMaintenance }
catch {
  $foreignPreserved = (
    (Test-Path -LiteralPath $MaintenancePath) -and
    (Get-Content -LiteralPath $MaintenancePath -Raw).Trim() -eq 'foreign-owner'
  )
}
if (-not $foreignPreserved) {
  throw "a foreign maintenance marker was not preserved"
}
Remove-Item -LiteralPath $MaintenancePath -Force

[pscustomobject]@{
  drain_state = $ack.gateway_state
  probe_calls = $script:ProbeCalls
  lease_seconds = $DrainLeaseSeconds
  timeout_enforced = $timedOut
  maintenance_collision_protected = $maintenanceCollisionProtected
  lifecycle_calls = $script:LifecycleCalls
  foreign_preserved = $foreignPreserved
} | ConvertTo-Json -Compress
'''.lstrip(),
        encoding="ascii",
    )

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            str(RESTART_HELPER),
            str(hermes_root),
            sys.executable,
            powershell,
            str(sleeper),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {
        "drain_state": "draining",
        "probe_calls": 4,
        "lease_seconds": 219,
        "timeout_enforced": True,
        "maintenance_collision_protected": True,
        "lifecycle_calls": ["stop", "wrapper-start"],
        "foreign_preserved": True,
    }
