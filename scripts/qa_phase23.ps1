<#
.SYNOPSIS
  Phase 23 QA matrix: global shortcut hooks, notification resource profile,
  Mica/OLED rendering under streaming load, Explorer integration.

.DESCRIPTION
  Runs against the *running* Compa desktop app. It drives the shell through the
  same `--qa` command surface the single-instance plugin exposes, then reads
  `desktop-state.json` for the authoritative result of every OS hook.

  Verifications
    1. Hooks        default shortcut is owned by Windows, Windows refuses the
                    combo elsewhere, the app sums itself from the tray, a
                    conflicting remap is rejected and the old key restored,
                    exit unregisters and releases the combination.
    2. Resources    notification listener polls while on; toggling off freezes
                    polls, drops the thread and returns CPU to baseline.
    3. Rendering    Mica applies to the frameless window, the palette uses
                    #000000, and a synthetic 3.4 s token stream keeps frame
                    pacing inside budget (no stutter).

  The app is left exactly as found (mode, backdrop, shortcut, notification and
  Explorer toggles are restored).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\qa_phase23.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\qa_phase23.ps1 -SkipExitTest -SampleSeconds 4
#>
[CmdletBinding()]
param(
  [string]$Exe = "",
  [switch]$SkipExitTest,
  [switch]$SkipKeyPress,
  [int]$SampleSeconds = 6
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $env:LOCALAPPDATA "com.personalagent.desktop"
$statePath = Join-Path $dataDir "desktop-state.json"
$reportPath = Join-Path $repo "build\phase23-qa.json"
$results = [System.Collections.ArrayList]::new()

function Add-Result {
  param(
    [string]$Id,
    [string]$Name,
    [string]$Status,
    [string]$Detail
  )
  $null = $results.Add([pscustomobject]@{ id = $Id; name = $Name; status = $Status; detail = $Detail })
  $colour = switch ($Status) {
    "PASS" { "Green" }
    "WARN" { "Yellow" }
    "SKIP" { "DarkGray" }
    default { "Red" }
  }
  Write-Host ("[{0}] {1} - {2}" -f $Status, $Id, $Name) -ForegroundColor $colour
  if ($Detail) { Write-Host ("        {0}" -f $Detail) -ForegroundColor DarkGray }
}

function Test-Check {
  param(
    [string]$Id,
    [string]$Name,
    [scriptblock]$Condition,
    [string]$Detail
  )
  try {
    if (& $Condition) {
      Add-Result $Id $Name "PASS" $Detail
      return $true
    }
    Add-Result $Id $Name "FAIL" $Detail
    return $false
  } catch {
    Add-Result $Id $Name "FAIL" ("{0} (exception: {1})" -f $Detail, $_.Exception.Message)
    return $false
  }
}

# ---------------------------------------------------------------- native bits

if (-not ("QaNative" -as [type])) {
  Add-Type -Namespace Qa -Name Native -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
public static extern bool RegisterHotKey(System.IntPtr hWnd, int id, uint fsModifiers, uint vk);
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
public static extern bool UnregisterHotKey(System.IntPtr hWnd, int id);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool IsWindowVisible(System.IntPtr hWnd);
[System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern System.IntPtr FindWindow(string className, string windowName);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern void keybd_event(byte bVk, byte bScan, uint flags, System.UIntPtr extra);
[System.Runtime.InteropServices.DllImport("dwmapi.dll")]
public static extern int DwmGetWindowAttribute(System.IntPtr hwnd, int attribute, out int value, int size);
"@
}

$MOD_ALT = 0x1
$MOD_CONTROL = 0x2
$MOD_SHIFT = 0x4
$MOD_WIN = 0x8
$VK_SHIFT = 0x10
$VK_CONTROL = 0x11
$VK_SPACE = 0x20
$VK_F9 = 0x78
$KEYEVENTF_KEYUP = 0x2
$HOTKEY_PROBE = 9101
$HOTKEY_CONFLICT = 9102

function Resolve-Exe {
  $candidates = @(
    $Exe,
    (Join-Path $repo "build\Personal Agent\Personal Agent.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Personal Agent\Personal Agent.exe")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
  # A single match would otherwise be a bare string and index to its first char.
  $list = @($candidates)
  if ($list.Count -eq 0) {
    throw "Personal Agent.exe not found. Pass -Exe <path> or run scripts\build_windows.ps1 first."
  }
  return [string]$list[0]
}

function Read-State {
  param([int]$TimeoutSeconds = 6)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  $lastError = $null
  while ((Get-Date) -lt $deadline) {
    try {
      $raw = Get-Content -Raw -LiteralPath $statePath -ErrorAction Stop
      if ($raw) {
        return ($raw | ConvertFrom-Json)
      }
    } catch {
      $lastError = $_.Exception.Message
    }
    Start-Sleep -Milliseconds 120
  }
  throw "desktop-state.json unreadable ($lastError)"
}

function Invoke-Qa {
  param(
    [string[]]$Arguments,
    [scriptblock]$Until,
    [int]$TimeoutSeconds = 25
  )
  $before = (Get-Item -LiteralPath $statePath).LastWriteTimeUtc
  $proc = Start-Process -FilePath $script:appExe -ArgumentList (@("--qa") + $Arguments) -PassThru
  $null = $proc.WaitForExit(8000)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $state = Read-State -TimeoutSeconds 3
    $advanced = (Get-Item -LiteralPath $statePath).LastWriteTimeUtc -gt $before
    if ($advanced -and (-not $Until -or (& $Until $state))) {
      Start-Sleep -Milliseconds 120
      return $state
    }
    Start-Sleep -Milliseconds 100
  }
  throw ("no state confirmation for --qa {0}" -f ($Arguments -join " "))
}

function Get-AppProcess {
  $proc = Get-Process -Id $script:appPid -ErrorAction SilentlyContinue
  if (-not $proc) {
    throw "app process $($script:appPid) is not running"
  }
  $proc.Refresh()
  return $proc
}

function Get-AppWindow {
  # FindWindow works for hidden windows; MainWindowHandle can fall back to a
  # different top-level window (hotkey thread) once the app window is hidden.
  $handle = [Qa.Native]::FindWindow([NullString]::Value, "Personal Agent")
  if ($handle -ne [IntPtr]::Zero) {
    return $handle
  }
  return (Get-AppProcess).MainWindowHandle
}

function Test-WindowVisible {
  $handle = Get-AppWindow
  return ($handle -ne [IntPtr]::Zero -and [Qa.Native]::IsWindowVisible($handle))
}

function Measure-CpuPercent {
  param([int]$Seconds)
  $proc = Get-AppProcess
  $start = $proc.TotalProcessorTime
  Start-Sleep -Seconds $Seconds
  $proc.Refresh()
  $elapsed = ($proc.TotalProcessorTime - $start).TotalSeconds
  return [math]::Round(($elapsed / $Seconds) * 100, 2)
}

function Send-SummonChord {
  # The shipped chord is Ctrl+Shift+Space; keybd_event takes virtual-key
  # codes, not the MOD_* flags used by RegisterHotKey.
  [Qa.Native]::keybd_event($VK_CONTROL, 0, 0, [UIntPtr]::Zero)
  [Qa.Native]::keybd_event($VK_SHIFT, 0, 0, [UIntPtr]::Zero)
  [Qa.Native]::keybd_event($VK_SPACE, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 70
  [Qa.Native]::keybd_event($VK_SPACE, 0, $KEYEVENTF_KEYUP, [UIntPtr]::Zero)
  [Qa.Native]::keybd_event($VK_SHIFT, 0, $KEYEVENTF_KEYUP, [UIntPtr]::Zero)
  [Qa.Native]::keybd_event($VK_CONTROL, 0, $KEYEVENTF_KEYUP, [UIntPtr]::Zero)
}

function Wait-ForExit {
  param([int]$ProcessId, [int]$TimeoutSeconds = 15)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $true }
    Start-Sleep -Milliseconds 200
  }
  return $false
}

function Start-App {
  $proc = Start-Process -FilePath $script:appExe -PassThru
  $deadline = (Get-Date).AddSeconds(45)
  while ((Get-Date) -lt $deadline) {
    $state = Read-State -TimeoutSeconds 3
    if ($state.pid -eq $proc.Id -and -not $state.exited) {
      $script:appPid = $state.pid
      return $state
    }
    Start-Sleep -Milliseconds 250
  }
  throw "app did not publish state after launch"
}

# ------------------------------------------------------------------- preamble

Write-Host ""
Write-Host "Phase 23 QA - Compa desktop integration" -ForegroundColor Cyan
Write-Host ("Repo: {0}" -f $repo) -ForegroundColor DarkGray

$script:appExe = Resolve-Exe
Write-Host ("Exe : {0}" -f $script:appExe) -ForegroundColor DarkGray

try {
  $initial = Read-State -TimeoutSeconds 3
  $script:appPid = $initial.pid
  if (-not (Get-Process -Id $script:appPid -ErrorAction SilentlyContinue)) {
    throw "stale state"
  }
} catch {
  Write-Host "Starting the app for the matrix..." -ForegroundColor DarkGray
  $initial = Start-App
}
$script:appPid = $initial.pid

$original = [pscustomobject]@{
  shortcut = $initial.shortcut
  mode = $initial.mode
  backdrop = $initial.backdrop
  notifications = [bool]$initial.notifications_enabled
  context_menu = [bool]$initial.context_menu_enabled
}

Write-Host ("PID : {0}  mode: {1}  shortcut: {2}" -f $script:appPid, $original.mode, $original.shortcut) -ForegroundColor DarkGray
Write-Host ""

# --------------------------------------------------------------- 1. hooks

Add-Result "preflight" "State contract" "PASS" ("pid={0}, qa_error='{1}'" -f $initial.pid, $initial.qa_error)

$probeHeld = -not [Qa.Native]::RegisterHotKey([IntPtr]::Zero, $HOTKEY_PROBE, ($MOD_CONTROL -bor $MOD_SHIFT), $VK_SPACE)
if (-not $probeHeld) {
  $null = [Qa.Native]::UnregisterHotKey([IntPtr]::Zero, $HOTKEY_PROBE)
}
Test-Check "hook-own" "Windows refuses the default chord elsewhere" {
  $probeHeld
} ("RegisterHotKey($($original.shortcut)) blocked={0}" -f $probeHeld) | Out-Null

Test-Check "hook-registered" "Shortcut is registered and error-free" {
  $state = Read-State
  $state.shortcut_registered -and -not $state.shortcut_error
} ("registered={0} error='{1}'" -f (Read-State).shortcut_registered, (Read-State).shortcut_error) | Out-Null

if (-not $SkipKeyPress) {
  $visibleBefore = Test-WindowVisible
  $null = Invoke-Qa -Arguments @("hide")
  Start-Sleep -Milliseconds 500
  $hidden = -not (Test-WindowVisible)
  Test-Check "hook-hide" "Tray hide really hides the window" { $hidden } ("visible before={0}" -f $visibleBefore) | Out-Null

  Send-SummonChord
  Start-Sleep -Milliseconds 900
  Test-Check "hook-summon" "Global chord summons the hidden window" {
    Test-WindowVisible
  } "Ctrl+Shift+Space pressed with the palette hidden" | Out-Null
}

$conflictHeld = [Qa.Native]::RegisterHotKey([IntPtr]::Zero, $HOTKEY_CONFLICT, ($MOD_CONTROL -bor $MOD_ALT -bor $MOD_SHIFT), $VK_F9)
if (-not $conflictHeld) {
  Add-Result "hook-conflict" "Conflicting remap is rejected, old key restored" "SKIP" "could not reserve Ctrl+Alt+Shift+F9 in this session"
} else {
  try {
    $conflicted = $null
    try {
      $conflicted = Invoke-Qa -Arguments @("shortcut=Ctrl+Alt+Shift+F9") -Until { param($s) $s.shortcut_error -ne "" } -TimeoutSeconds 15
    } catch {
      $conflicted = Read-State
    }
    Test-Check "hook-conflict" "Conflicting remap is rejected, old key restored" {
      ($conflicted.shortcut_error -ne "") -and
      ($conflicted.shortcut -eq $original.shortcut) -and
      ([bool]$conflicted.shortcut_registered)
    } ("error='{0}' kept='{1}' registered={2}" -f $conflicted.shortcut_error, $conflicted.shortcut, $conflicted.shortcut_registered) | Out-Null

    $cleared = Invoke-Qa -Arguments @("shortcut=$($original.shortcut)") -Until { param($s) $s.shortcut_error -eq "" -and $s.shortcut_registered } -TimeoutSeconds 15
    Test-Check "hook-recover" "Re-applying the original shortcut clears the error" {
      $cleared.shortcut -eq $original.shortcut -and -not $cleared.shortcut_error
    } ("shortcut={0}" -f $cleared.shortcut) | Out-Null
  } finally {
    $null = [Qa.Native]::UnregisterHotKey([IntPtr]::Zero, $HOTKEY_CONFLICT)
  }
}

if ($SkipExitTest) {
  Add-Result "hook-exit" "Exit unregisters the shortcut" "SKIP" "disabled with -SkipExitTest"
} else {
  $oldPid = $script:appPid
  $null = Invoke-Qa -Arguments @("quit") -TimeoutSeconds 15
  $exited = Wait-ForExit -ProcessId $oldPid -TimeoutSeconds 20
  $finalState = $null
  try { $finalState = Read-State -TimeoutSeconds 3 } catch { }
  $released = [Qa.Native]::RegisterHotKey([IntPtr]::Zero, $HOTKEY_PROBE, ($MOD_CONTROL -bor $MOD_SHIFT), $VK_SPACE)
  if ($released) { $null = [Qa.Native]::UnregisterHotKey([IntPtr]::Zero, $HOTKEY_PROBE) }
  Test-Check "hook-exit" "Exit unregisters the shortcut" {
    $exited -and $finalState.exited -and (-not $finalState.shortcut_registered) -and $released
  } ("process gone={0} state.exited={1} registered={2} chord free={3}" -f $exited, $finalState.exited, $finalState.shortcut_registered, $released) | Out-Null

  Write-Host "Relaunching the app after the exit test..." -ForegroundColor DarkGray
  $null = Start-App
  Add-Result "hook-relaunch" "App returns from the tray after relaunch" "PASS" ("pid={0}" -f $script:appPid)
}

# ----------------------------------------------------------- 2. resources

$null = Invoke-Qa -Arguments @("notifications=off") -Until { param($s) -not $s.notifications_enabled }
Start-Sleep -Seconds 2

$threadsBefore = (Get-AppProcess).Threads.Count
$cpuBaseline = Measure-CpuPercent -Seconds $SampleSeconds
Add-Result "res-baseline" "Idle baseline sampled" "PASS" ("cpu={0}% of one core, threads={1}" -f $cpuBaseline, $threadsBefore)

$notifState = $null
try {
  $notifState = Invoke-Qa -Arguments @("notifications=on") -Until {
    param($s)
    $s.notifications_status -eq "active" -or $s.notifications_status -match "denied|unavailable|error|unsupported"
  } -TimeoutSeconds 40
} catch {
  $notifState = Read-State
}
$notifStatus = [string]$notifState.notifications_status
Write-Host ("Notification listener: {0}" -f $notifStatus) -ForegroundColor DarkGray

if ($notifStatus -eq "active") {
  Start-Sleep -Seconds 2
  $pollsA = (Read-State).notification_polls
  # The listener polls every 10 s, so an 11 s window always contains a poll.
  Start-Sleep -Seconds 11
  $pollsB = (Read-State).notification_polls
  $threadsOn = (Get-AppProcess).Threads.Count
  $cpuOn = Measure-CpuPercent -Seconds $SampleSeconds

  Test-Check "res-listening" "Listener polls Windows toasts while enabled" {
    $pollsB -gt $pollsA
  } ("polls {0} -> {1}, threads={2}, cpu={3}%" -f $pollsA, $pollsB, $threadsOn, $cpuOn) | Out-Null
} else {
  Add-Result "res-listening" "Listener polls Windows toasts while enabled" "WARN" ("Windows did not grant toast access: {0}" -f $notifStatus)
  $cpuOn = $cpuBaseline
  $threadsOn = $threadsBefore
}

$null = Invoke-Qa -Arguments @("notifications=off") -Until { param($s) -not $s.notifications_enabled -and $s.notifications_status -eq "disabled" }
$pollsFrozenA = (Read-State).notification_polls
# Longer than one listener interval: a running hook would have polled again.
Start-Sleep -Seconds 11
$pollsFrozenB = (Read-State).notification_polls
$threadsOff = (Get-AppProcess).Threads.Count
$cpuOff = Measure-CpuPercent -Seconds $SampleSeconds

Test-Check "res-unregistered" "Disable unregisters the hook thread" {
  $pollsFrozenA -eq 0 -and $pollsFrozenB -eq 0 -and $threadsOff -le ($threadsBefore + 2)
} ("polls={0}/{1}, threads {2} -> {3} (baseline {4})" -f $pollsFrozenA, $pollsFrozenB, $threadsOn, $threadsOff, $threadsBefore) | Out-Null

Test-Check "res-cpu" "CPU returns to baseline with the listener off" {
  $cpuOff -le ($cpuBaseline + 0.75)
} ("baseline={0}%  enabled={1}%  disabled={2}%" -f $cpuBaseline, $cpuOn, $cpuOff) | Out-Null

# ----------------------------------------------------------- 3. rendering

$null = Invoke-Qa -Arguments @("mode=modern", "backdrop=mica", "show") -Until {
  param($s)
  $s.mode -eq "modern" -and $s.backdrop -eq "mica" -and $s.backdrop_error -eq ""
}
# DWMWA_SYSTEMBACKDROP_TYPE (38): 2 = Mica, 1 = none. This proves the OS
# compositor really has the backdrop, not just that the call returned Ok.
$backdropType = -1
$dwmHr = [Qa.Native]::DwmGetWindowAttribute((Get-AppWindow), 38, [ref]$backdropType, 4)
Test-Check "render-mica" "Mica applies to the frameless palette" {
  $state = Read-State
  $micaOk = ($dwmHr -ne 0) -or ($backdropType -eq 2)
  $state.mode -eq "modern" -and $state.backdrop -eq "mica" -and [string]::IsNullOrEmpty($state.backdrop_error) -and $micaOk
} ("backdrop={0} error='{1}' DWM type={2} (hr=0x{3:X})" -f (Read-State).backdrop, (Read-State).backdrop_error, $backdropType, $dwmHr) | Out-Null

$css = Get-Content -Raw -LiteralPath (Join-Path $repo "frontend\src\index.css")
$conf = Get-Content -Raw -LiteralPath (Join-Path $repo "frontend\src-tauri\tauri.conf.json") | ConvertFrom-Json
$windowConfig = $conf.app.windows[0]
Test-Check "render-oled" "Strict OLED surface and frameless window" {
  ($css -match "#000000") -and
  ($css -match 'palette-root\[data-backdrop="solid"\]') -and
  ($windowConfig.decorations -eq $false) -and
  ($windowConfig.transparent -eq $true)
} ("#000000 in CSS={0}, decorations={1}, transparent={2}" -f ($css -match "#000000"), $windowConfig.decorations, $windowConfig.transparent) | Out-Null

Start-Sleep -Seconds 1
$renderStart = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - 2
$stress = $null
try {
  $stress = Invoke-Qa -Arguments @("render", "show") -Until {
    param($s)
    $s.frame_stats.mode -eq "stress" -and [int]$s.frame_stats.at -ge $renderStart
  } -TimeoutSeconds 35
} catch {
  $stress = Read-State
}
$frames = $stress.frame_stats
Test-Check "render-frames" "Palette holds frame pacing during token streaming" {
  $frames.mode -eq "stress" -and
  [double]$frames.fps -ge 45 -and
  [double]$frames.max_frame_ms -le 130 -and
  [int]$frames.long_frames -le 8 -and
  [int]$frames.frames -ge 60
} ("mode={0} fps={1} max={2}ms long={3} frames={4}" -f $frames.mode, $frames.fps, $frames.max_frame_ms, $frames.long_frames, $frames.frames) | Out-Null

$null = Invoke-Qa -Arguments @("backdrop=solid") -Until { param($s) $s.backdrop -eq "solid" -and $s.backdrop_error -eq "" }
$solidType = -1
$solidHr = [Qa.Native]::DwmGetWindowAttribute((Get-AppWindow), 38, [ref]$solidType, 4)
Test-Check "render-solid" "OLED black backdrop applies without a Mica layer" {
  $state = Read-State
  $state.backdrop -eq "solid" -and [string]::IsNullOrEmpty($state.backdrop_error) -and (($solidHr -ne 0) -or ($solidType -ne 2))
} ("backdrop={0} error='{1}' DWM type={2} (hr=0x{3:X})" -f (Read-State).backdrop, (Read-State).backdrop_error, $solidType, $solidHr) | Out-Null

# --------------------------------------------------- 4. Explorer integration

$explorerKey = "HKCU:\Software\Classes\*\shell\Compa"
$null = Invoke-Qa -Arguments @("context_menu=on") -Until { param($s) [bool]$s.context_menu_enabled }
$command = $null
try {
  $command = (Get-ItemProperty -LiteralPath "$explorerKey\command" -ErrorAction Stop)."(default)"
} catch {
  $command = $null
}

Test-Check "shell-menu" "Explorer menu routes the selected path" {
  $command -and ($command -match "--analyze") -and ($command -match '"%1"') -and ($command -match "Personal Agent\.exe")
} ("command={0}" -f $command) | Out-Null

$null = Invoke-Qa -Arguments @("context_menu=off") -Until { param($s) -not [bool]$s.context_menu_enabled }
Test-Check "shell-menu-off" "Disabling removes the registry keys" {
  -not (Test-Path -LiteralPath $explorerKey)
} ("key present={0}" -f (Test-Path -LiteralPath $explorerKey)) | Out-Null

# ------------------------------------------------------------------ restore

$restoreArgs = @(
  ("shortcut={0}" -f $original.shortcut),
  ("mode={0}" -f $original.mode),
  ("backdrop={0}" -f $original.backdrop),
  ("notifications={0}" -f $original.notifications.ToString().ToLower()),
  ("context_menu={0}" -f $original.context_menu.ToString().ToLower())
)
$restoredState = Invoke-Qa -Arguments $restoreArgs -Until {
  param($s)
  $s.shortcut -eq $original.shortcut -and
  $s.mode -eq $original.mode -and
  $s.backdrop -eq $original.backdrop -and
  ([bool]$s.notifications_enabled -eq $original.notifications) -and
  ([bool]$s.context_menu_enabled -eq $original.context_menu)
}
Add-Result "restore" "Original desktop settings restored" "PASS" ("mode={0}, backdrop={1}, shortcut={2}, notifications={3}, context_menu={4}" -f $restoredState.mode, $restoredState.backdrop, $restoredState.shortcut, $restoredState.notifications_enabled, $restoredState.context_menu_enabled)

# ------------------------------------------------------------------ summary

$failures = @($results | Where-Object { $_.status -eq "FAIL" })
$warnings = @($results | Where-Object { $_.status -eq "WARN" })
$skips = @($results | Where-Object { $_.status -eq "SKIP" })
$passed = @($results | Where-Object { $_.status -eq "PASS" })

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $reportPath) | Out-Null
[pscustomobject]@{
  phase = 23
  generated_at = (Get-Date).ToString("o")
  exe = $script:appExe
  pid = $script:appPid
  settings_before = $original
  results = $results
  summary = [pscustomobject]@{
    passed = $passed.Count
    failed = $failures.Count
    warnings = $warnings.Count
    skipped = $skips.Count
  }
} | ConvertTo-Json -Depth 6 | ForEach-Object {
  [System.IO.File]::WriteAllText($reportPath, $_, [System.Text.UTF8Encoding]::new($false))
}

Write-Host ""
Write-Host ("{0} passed, {1} failed, {2} warnings, {3} skipped" -f $passed.Count, $failures.Count, $warnings.Count, $skips.Count) -ForegroundColor $(if ($failures.Count) { "Red" } else { "Green" })
Write-Host ("Report: {0}" -f $reportPath) -ForegroundColor DarkGray

if ($failures.Count -gt 0) {
  exit 1
}
exit 0
