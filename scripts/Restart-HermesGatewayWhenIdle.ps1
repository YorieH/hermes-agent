param(
  [Parameter(Mandatory=$true)]
  [ValidateSet('kurumi', 'asuna', 'kairi', 'rikku', 'yuna')]
  [string]$Profile,
  [ValidateRange(1, 86400)][int]$TimeoutSeconds = 7200,
  [ValidateRange(1, 3600)][int]$PollSeconds = 30,
  [ValidateRange(2, 10)][int]$MinimumIdleSamples = 2,
  [ValidateRange(30, 3600)][int]$MaximumStateAgeSeconds = 300,
  [ValidateRange(5, 300)][int]$DrainAcknowledgeTimeoutSeconds = 45,
  [ValidateRange(30, 300)][int]$RestartCommandTimeoutSeconds = 120,
  [ValidateRange(10, 300)][int]$PostRestartTimeoutSeconds = 60,
  [switch]$DryRun,
  [string]$HermesRoot = (Join-Path $env:LOCALAPPDATA "hermes"),
  [string]$HermesPython = "",
  [string]$HermesExecutable = ""
)

$ErrorActionPreference = "Stop"
$OperationId = "idle-restart:${Profile}:$([guid]::NewGuid().ToString('N'))"
$DefaultHermesRoot = Join-Path $env:LOCALAPPDATA "hermes"
$HermesRoot = [IO.Path]::GetFullPath($HermesRoot)
$ProfilesRoot = Join-Path $HermesRoot "profiles"
if (-not $HermesPython) {
  $HermesPython = Join-Path $HermesRoot "hermes-agent\venv\Scripts\python.exe"
}
if (-not $HermesExecutable) {
  $HermesExecutable = Join-Path $HermesRoot "hermes-agent\venv\Scripts\hermes.exe"
}
$HermesPython = [IO.Path]::GetFullPath($HermesPython)
$HermesExecutable = [IO.Path]::GetFullPath($HermesExecutable)
$ProfileHome = Join-Path $ProfilesRoot $Profile
$GuardScript = Join-Path $HermesRoot "scripts\hermes_idle_restart_guard.py"
$LogDir = Join-Path $HermesRoot "logs"
$LogPath = Join-Path $LogDir "Restart-HermesGatewayWhenIdle-$Profile.log"
$DrainLeaseSeconds = (
  $DrainAcknowledgeTimeoutSeconds +
  $RestartCommandTimeoutSeconds +
  (2 * $PostRestartTimeoutSeconds) +
  120
)

$TargetBindingEstablished = $false

function ConvertTo-SafeLogText {
  param([string]$Message)
  $safe = [string]$Message -replace "[\r\n]+", " "
  $safe = $safe -replace [regex]::Escape($HermesRoot), "<HERMES_ROOT>"
  $safe = $safe -replace "[0-9]{8,12}:[A-Za-z0-9_-]{20,}", "<TELEGRAM_BOT_TOKEN>"
  if ($safe.Length -gt 500) { $safe = $safe.Substring(0, 500) }
  return $safe
}

function Write-IdleRestartLog {
  param([string]$Message)
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  $safe = ConvertTo-SafeLogText $Message
  Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "$stamp operation=$OperationId $safe"
}

function Assert-LiveTargetBinding {
  foreach ($path in @($HermesPython, $HermesExecutable, $GuardScript)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required Hermes executable or guard path is missing" }
  }
  if (-not (Test-Path -LiteralPath $ProfileHome -PathType Container)) { throw "Required Hermes profile directory is missing" }
  if ($env:_HERMES_GATEWAY -eq "1") {
    throw "Inherited gateway context is forbidden; use Schedule-HermesGatewayRestartWhenIdle.ps1"
  }
  if (-not $DryRun) {
    $expectedRoot = [IO.Path]::GetFullPath($DefaultHermesRoot)
    $expectedPython = [IO.Path]::GetFullPath((Join-Path $expectedRoot "hermes-agent\venv\Scripts\python.exe"))
    $expectedExecutable = [IO.Path]::GetFullPath((Join-Path $expectedRoot "hermes-agent\venv\Scripts\hermes.exe"))
    if ($HermesRoot -ne $expectedRoot -or $HermesPython -ne $expectedPython -or $HermesExecutable -ne $expectedExecutable) {
      throw "Non-dry-run custom Hermes roots or executables are forbidden"
    }
  }
  $script:TargetBindingEstablished = $true
}

function Invoke-TargetNativeCommand {
  param(
    [Parameter(Mandatory=$true)][string]$FilePath,
    [string[]]$Arguments = @()
  )
  $hadProfile = Test-Path -LiteralPath Env:HERMES_PROFILE
  $hadHome = Test-Path -LiteralPath Env:HERMES_HOME
  $previousProfile = $env:HERMES_PROFILE
  $previousHome = $env:HERMES_HOME
  try {
    $env:HERMES_PROFILE = $Profile
    $env:HERMES_HOME = $ProfileHome
    $output = @(& $FilePath @Arguments 2>&1)
    $code = $LASTEXITCODE
    return [pscustomobject]@{
      Output = @($output)
      ExitCode = [int]$code
    }
  } finally {
    if ($hadProfile) { $env:HERMES_PROFILE = $previousProfile }
    else { Remove-Item -LiteralPath Env:HERMES_PROFILE -ErrorAction SilentlyContinue }
    if ($hadHome) { $env:HERMES_HOME = $previousHome }
    else { Remove-Item -LiteralPath Env:HERMES_HOME -ErrorAction SilentlyContinue }
  }
}

function Invoke-TargetNativeCommandWithTimeout {
  param(
    [Parameter(Mandatory=$true)][string]$FilePath,
    [string[]]$Arguments = @(),
    [Parameter(Mandatory=$true)][int]$TimeoutSeconds
  )
  $hadProfile = Test-Path -LiteralPath Env:HERMES_PROFILE
  $hadHome = Test-Path -LiteralPath Env:HERMES_HOME
  $previousProfile = $env:HERMES_PROFILE
  $previousHome = $env:HERMES_HOME
  $token = [guid]::NewGuid().ToString('N')
  $stdoutPath = Join-Path ([IO.Path]::GetTempPath()) "hermes-idle-restart-$token.stdout"
  $stderrPath = Join-Path ([IO.Path]::GetTempPath()) "hermes-idle-restart-$token.stderr"
  $process = $null
  try {
    $env:HERMES_PROFILE = $Profile
    $env:HERMES_HOME = $ProfileHome
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
      -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath `
      -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
      try { $process.Kill() } catch {}
      try { $process.WaitForExit() } catch {}
      throw "Native command exceeded its $TimeoutSeconds second safety timeout"
    }
    # A second zero-argument wait flushes asynchronous redirected output after
    # the process handle becomes signaled on Windows PowerShell 5.1.
    $process.WaitForExit()
    $stdout = if (Test-Path -LiteralPath $stdoutPath) { @(Get-Content -LiteralPath $stdoutPath -Encoding UTF8) } else { @() }
    $stderr = if (Test-Path -LiteralPath $stderrPath) { @(Get-Content -LiteralPath $stderrPath -Encoding UTF8) } else { @() }
    return [pscustomobject]@{
      Output = @($stdout) + @($stderr)
      ExitCode = [int]$process.ExitCode
    }
  } finally {
    if ($hadProfile) { $env:HERMES_PROFILE = $previousProfile }
    else { Remove-Item -LiteralPath Env:HERMES_PROFILE -ErrorAction SilentlyContinue }
    if ($hadHome) { $env:HERMES_HOME = $previousHome }
    else { Remove-Item -LiteralPath Env:HERMES_HOME -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    if ($null -ne $process) { $process.Dispose() }
  }
}

function Invoke-GuardJson {
  param([string[]]$Arguments)
  $invocation = Invoke-TargetNativeCommand -FilePath $HermesPython -Arguments (@($GuardScript) + $Arguments)
  $output = @($invocation.Output)
  $code = $invocation.ExitCode
  if ($code -ne 0) {
    $detail = if ($output.Count) { ConvertTo-SafeLogText ([string]$output[-1]) } else { "no diagnostic" }
    throw "Idle guard exited $code ($detail)"
  }
  try {
    return ([string]$output[-1] | ConvertFrom-Json)
  } catch {
    throw "Idle guard returned invalid JSON"
  }
}

function Get-IdleSnapshot {
  param(
    [string[]]$AllowedStates = @("running"),
    [long]$ExpectedPid = 0,
    [long]$ExpectedStartTime = 0,
    [switch]$RequireTelegramConnected
  )
  $arguments = @(
    "probe", "--root", $HermesRoot, "--profile", $Profile,
    "--expected-executable", $HermesPython,
    "--maximum-state-age", "$MaximumStateAgeSeconds"
  )
  foreach ($state in $AllowedStates) { $arguments += @("--allowed-state", $state) }
  if ($ExpectedPid -gt 0) { $arguments += @("--expected-pid", "$ExpectedPid") }
  if ($ExpectedStartTime -gt 0) { $arguments += @("--expected-start-time", "$ExpectedStartTime") }
  if ($RequireTelegramConnected) { $arguments += "--require-telegram-connected" }
  return Invoke-GuardJson -Arguments $arguments
}

function Enter-OwnedDrain {
  $null = Invoke-GuardJson -Arguments @(
    "drain-begin", "--home", $ProfileHome, "--principal", $OperationId,
    "--owner-pid", "$PID", "--lease-seconds", "$DrainLeaseSeconds"
  )
  Write-IdleRestartLog "external_drain_claimed profile=$Profile owner_pid=$PID lease_seconds=$DrainLeaseSeconds"
}

function Exit-OwnedDrain {
  $null = Invoke-GuardJson -Arguments @(
    "drain-clear", "--home", $ProfileHome, "--principal", $OperationId
  )
  Write-IdleRestartLog "external_drain_released profile=$Profile"
}

function Assert-OwnedDrain {
  $status = Invoke-GuardJson -Arguments @("drain-status", "--home", $ProfileHome)
  if ($null -eq $status.marker -or [string]$status.marker.principal -ne $OperationId) {
    throw "The external drain marker is no longer owned by this operation"
  }
  if ([long]$status.marker.owner_pid -ne [long]$PID) {
    throw "The external drain marker owner PID does not match this operation"
  }
  if (-not $status.marker.lease_id -or -not $status.marker.lease_expires_at) {
    throw "The external drain marker is missing its bounded lease"
  }
  try { $leaseExpiry = [DateTimeOffset]::Parse([string]$status.marker.lease_expires_at) }
  catch { throw "The external drain marker has an invalid lease expiry" }
  if ($leaseExpiry -le [DateTimeOffset]::UtcNow) {
    throw "The external drain marker lease has expired"
  }
}

function Invoke-GracefulProfileRestart {
  $invocation = Invoke-TargetNativeCommandWithTimeout `
    -FilePath $HermesExecutable `
    -Arguments @("--profile", $Profile, "gateway", "restart") `
    -TimeoutSeconds $RestartCommandTimeoutSeconds
  $output = @($invocation.Output)
  $code = $invocation.ExitCode
  if ($code -ne 0) { throw "Graceful Hermes gateway restart exited $code" }
  Write-IdleRestartLog "graceful_restart_command_complete profile=$Profile output_lines=$($output.Count)"
}

function Wait-ForExternalDrainIdle {
  param([int]$GatewayPid, [long]$StartTime)
  $deadline = (Get-Date).AddSeconds($DrainAcknowledgeTimeoutSeconds)
  $samples = 0
  $lastProbeError = ""
  while ((Get-Date) -lt $deadline) {
    try {
      Assert-OwnedDrain
      $snapshot = Get-IdleSnapshot -AllowedStates @("draining") -ExpectedPid $GatewayPid -ExpectedStartTime $StartTime
      $lastProbeError = ""
    } catch {
      # The gateway observes the external drain marker asynchronously. Do not
      # count idle samples until persisted state proves that it has stopped
      # accepting new turns by entering "draining".
      $lastProbeError = $_.Exception.Message
      $samples = 0
      Start-Sleep -Seconds 1
      continue
    }
    if ($snapshot.active_agents -eq 0 -and $snapshot.kanban_busy -eq 0) {
      $samples += 1
      if ($samples -ge $MinimumIdleSamples) { return $snapshot }
    } else {
      $samples = 0
    }
    Start-Sleep -Seconds 1
  }
  $suffix = if ($lastProbeError) { " (last probe: $lastProbeError)" } else { "" }
  throw "External drain did not acknowledge a stable idle state in time$suffix"
}

function Wait-ForReplacement {
  param([int]$PriorPid, [long]$PriorStartTime, [string[]]$AllowedStates)
  $deadline = (Get-Date).AddSeconds($PostRestartTimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $snapshot = Get-IdleSnapshot -AllowedStates $AllowedStates
      if ($snapshot.pid -ne $PriorPid -or $snapshot.start_time -ne $PriorStartTime) {
        return $snapshot
      }
    } catch {
      Write-IdleRestartLog "replacement_wait retry=$($_.Exception.GetType().Name)"
    }
    Start-Sleep -Seconds 1
  }
  throw "A unique replacement gateway did not become observable in time"
}

function Wait-ForRunningReplacement {
  param([int]$GatewayPid, [long]$StartTime)
  $deadline = (Get-Date).AddSeconds($PostRestartTimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      return Get-IdleSnapshot -AllowedStates @("running") -ExpectedPid $GatewayPid -ExpectedStartTime $StartTime -RequireTelegramConnected
    } catch {
      Write-IdleRestartLog "running_wait retry=$($_.Exception.GetType().Name)"
    }
    Start-Sleep -Seconds 1
  }
  throw "The replacement gateway did not return to running with Telegram connected"
}

function Invoke-IdleRestartMain {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  $idleSamples = 0
  $identity = $null
  $disruptiveSequenceStarted = $false
  Write-IdleRestartLog "watch_start profile=$Profile timeout_seconds=$TimeoutSeconds poll_seconds=$PollSeconds minimum_idle_samples=$MinimumIdleSamples dry_run=$([bool]$DryRun)"

  while ((Get-Date) -lt $deadline) {
    try {
      $snapshot = Get-IdleSnapshot -AllowedStates @("running") -RequireTelegramConnected
      if ($null -eq $identity -or $identity.pid -ne $snapshot.pid -or $identity.start_time -ne $snapshot.start_time) {
        $identity = [pscustomobject]@{ pid=[int]$snapshot.pid; start_time=[long]$snapshot.start_time }
        $idleSamples = 0
        Write-IdleRestartLog "gateway_identity_observed pid=$($identity.pid)"
      }
      Write-IdleRestartLog "state active_agents=$($snapshot.active_agents) kanban_busy=$($snapshot.kanban_busy) databases=$($snapshot.databases_checked) pid=$($snapshot.pid)"
      if ($snapshot.active_agents -eq 0 -and $snapshot.kanban_busy -eq 0) {
        $idleSamples += 1
        Write-IdleRestartLog "idle_sample count=$idleSamples required=$MinimumIdleSamples"
        if ($idleSamples -ge $MinimumIdleSamples) {
          $confirm = Get-IdleSnapshot -AllowedStates @("running") -ExpectedPid $identity.pid -ExpectedStartTime $identity.start_time -RequireTelegramConnected
          if ($confirm.active_agents -ne 0 -or $confirm.kanban_busy -ne 0) {
            $idleSamples = 0
          } elseif ($DryRun) {
            Write-IdleRestartLog "dry_run_idle_confirmed profile=$Profile"
            return 0
          } else {
            $disruptiveSequenceStarted = $true
            $drainOwned = $false
            try {
              Enter-OwnedDrain
              $drainOwned = $true
              $null = Wait-ForExternalDrainIdle -GatewayPid $identity.pid -StartTime $identity.start_time
              Write-IdleRestartLog "external_drain_idle_confirmed profile=$Profile"
              Assert-OwnedDrain
              $finalIdle = Get-IdleSnapshot -AllowedStates @("draining") -ExpectedPid $identity.pid -ExpectedStartTime $identity.start_time
              if ($finalIdle.active_agents -ne 0 -or $finalIdle.kanban_busy -ne 0) {
                throw "The target stopped being idle before the graceful restart"
              }
              Invoke-GracefulProfileRestart
              Assert-OwnedDrain
              $replacement = Wait-ForReplacement -PriorPid $identity.pid -PriorStartTime $identity.start_time -AllowedStates @("draining", "running")
              Exit-OwnedDrain
              $drainOwned = $false
              $running = Wait-ForRunningReplacement -GatewayPid ([int]$replacement.pid) -StartTime ([long]$replacement.start_time)
              Write-IdleRestartLog "watch_complete profile=$Profile new_pid=$($running.pid) telegram=$($running.telegram_state)"
              return 0
            } finally {
              if ($drainOwned) {
                try { Exit-OwnedDrain } catch { Write-IdleRestartLog "critical_owned_drain_release_failed type=$($_.Exception.GetType().Name)" }
              }
            }
          }
        }
      } else {
        $idleSamples = 0
      }
    } catch {
      $idleSamples = 0
      Write-IdleRestartLog "watch_error type=$($_.Exception.GetType().Name) message=$($_.Exception.Message)"
      if ($disruptiveSequenceStarted) { throw }
    }
    Start-Sleep -Seconds $PollSeconds
  }
  Write-IdleRestartLog "watch_timeout profile=$Profile"
  return 1
}

if ($MyInvocation.InvocationName -eq ".") {
  # Dot-sourcing loads the functions for behavioral tests and operator
  # diagnostics without starting a restart operation or exiting the caller.
  return
}

$mutex = New-Object System.Threading.Mutex($false, "Local\Haru.Hermes.IdleRestart.$Profile")
$mutexOwned = $false
try {
  Assert-LiveTargetBinding
  try { $mutexOwned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $mutexOwned = $true }
  if (-not $mutexOwned) { throw "Another idle-restart helper already owns this profile" }
  $exitCode = Invoke-IdleRestartMain
} catch {
  if ($TargetBindingEstablished) {
    Write-IdleRestartLog "fatal type=$($_.Exception.GetType().Name) message=$($_.Exception.Message)"
  } else {
    [Console]::Error.WriteLine("Idle restart refused before target binding: $($_.Exception.Message)")
  }
  $exitCode = 1
} finally {
  if ($mutexOwned) { try { $mutex.ReleaseMutex() } catch {} }
  $mutex.Dispose()
}
exit $exitCode
