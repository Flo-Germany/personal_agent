<#
.SYNOPSIS
  Phase 24 visual QA matrix for Compa.

.DESCRIPTION
  Drives the packaged (or dev) app through every core UI state, captures
  high-DPI screenshots, and collects the in-app layout/contrast audit plus
  webview console errors. Then it measures the palette expand/collapse
  animation and asks the local vision model to judge the pixels.

  Exit code 0 means: every state audited clean, no console errors, smooth
  height transitions, and the vision model passed every screenshot.

.PARAMETER Exe
  Path to Personal Agent.exe (default: build\Personal Agent\...).

.PARAMETER SkipVision
  Capture and audit only; do not start the vision model.

.PARAMETER SkipAnimation
  Skip the expand/collapse timing probe.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\visual_qa.ps1
#>
[CmdletBinding()]
param(
  [string]$Exe = "",
  [string]$OutDir = "build\phase24",
  [switch]$SkipVision,
  [switch]$SkipAnimation,
  [ValidateSet("dark", "light")]
  [string]$Theme = "dark",
  [int]$Threshold = 8
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $env:LOCALAPPDATA "com.personalagent.desktop"
$statePath = Join-Path $dataDir "desktop-state.json"
$shotsDir = Join-Path $repo (Join-Path $OutDir "shots")
$reportPath = Join-Path $repo (Join-Path $OutDir "qa-report.json")
$failures = [System.Collections.ArrayList]::new()
$warnings = [System.Collections.ArrayList]::new()

function Add-Failure([string]$message) {
  $null = $failures.Add($message)
  Write-Host "[FAIL] $message" -ForegroundColor Red
}
function Add-Warning([string]$message) {
  $null = $warnings.Add($message)
  Write-Host "[WARN] $message" -ForegroundColor Yellow
}
function Add-Pass([string]$message) {
  Write-Host "[PASS] $message" -ForegroundColor Green
}

if (-not ("VisualQaNative" -as [type])) {
  Add-Type -AssemblyName System.Drawing
  Add-Type -Namespace VisualQa -Name Native -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern System.IntPtr FindWindow(string className, string windowName);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool GetWindowRect(System.IntPtr hWnd, out RECT rect);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SetWindowPos(System.IntPtr hWnd, System.IntPtr after, int x, int y, int cx, int cy, uint flags);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool ShowWindow(System.IntPtr hWnd, int command);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SetProcessDPIAware();
[System.Runtime.InteropServices.DllImport("winmm.dll")]
public static extern uint timeBeginPeriod(uint milliseconds);
[System.Runtime.InteropServices.DllImport("winmm.dll")]
public static extern uint timeEndPeriod(uint milliseconds);
[System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
"@
}
$null = [VisualQa.Native]::SetProcessDPIAware()

function Resolve-Exe {
  $candidates = @(
    $Exe,
    (Join-Path $repo "build\Personal Agent\Personal Agent.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Personal Agent\Personal Agent.exe")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
  $list = @($candidates)
  if ($list.Count -eq 0) { throw "Personal Agent.exe not found." }
  return [string]$list[0]
}

function Read-State {
  param([int]$TimeoutSeconds = 6)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $raw = Get-Content -Raw -LiteralPath $statePath -ErrorAction Stop
      if ($raw) { return ($raw | ConvertFrom-Json) }
    } catch { }
    Start-Sleep -Milliseconds 100
  }
  throw "desktop-state.json unreadable"
}

function Invoke-Qa {
  param([string[]]$Arguments, [scriptblock]$Until, [int]$TimeoutSeconds = 25)
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

function Get-AppWindow {
  $handle = [VisualQa.Native]::FindWindow([NullString]::Value, "Personal Agent")
  if ($handle -eq [IntPtr]::Zero) { throw "app window not found" }
  return $handle
}

function Get-WindowHeight {
  $rect = New-Object VisualQa.Native+RECT
  if (-not [VisualQa.Native]::GetWindowRect((Get-AppWindow), [ref]$rect)) { return 0 }
  return $rect.Bottom - $rect.Top
}

function Capture-Window {
  param([string]$Path)
  $handle = Get-AppWindow
  $HWND_TOPMOST = [IntPtr](-1)
  $SWP_FLAGS = 0x0001 -bor 0x0002 -bor 0x0040
  $null = [VisualQa.Native]::ShowWindow($handle, 9)
  $null = [VisualQa.Native]::SetWindowPos($handle, $HWND_TOPMOST, 0, 0, 0, 0, $SWP_FLAGS)
  $null = [VisualQa.Native]::SetForegroundWindow($handle)
  Start-Sleep -Milliseconds 350

  $rect = New-Object VisualQa.Native+RECT
  if (-not [VisualQa.Native]::GetWindowRect($handle, [ref]$rect)) { throw "GetWindowRect failed" }
  $width = $rect.Right - $rect.Left
  $height = $rect.Bottom - $rect.Top
  $bitmap = New-Object System.Drawing.Bitmap $width, $height
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  try {
    $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
  } finally {
    $graphics.Dispose()
  }
  $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
  $bitmap.Dispose()
  $null = [VisualQa.Native]::SetWindowPos($handle, [IntPtr](-2), 0, 0, 0, 0, $SWP_FLAGS)
  return @{ width = $width; height = $height }
}

# ------------------------------------------------------------------ preamble
$script:appExe = Resolve-Exe
Write-Host "Visual QA" -ForegroundColor Cyan
Write-Host "Exe: $script:appExe" -ForegroundColor DarkGray

try {
  $initial = Read-State -TimeoutSeconds 3
  if (-not (Get-Process -Id $initial.pid -ErrorAction SilentlyContinue)) { throw "stale" }
} catch {
  Write-Host "Starting the app..." -ForegroundColor DarkGray
  Start-Process -FilePath $script:appExe | Out-Null
  Start-Sleep -Seconds 6
  $initial = Read-State
}
$script:appPid = $initial.pid
$original = [pscustomobject]@{
  mode = $initial.mode
  backdrop = $initial.backdrop
  theme = if ($initial.theme) { $initial.theme } else { "system" }
  animations = if ($null -ne $initial.animations) { [bool]$initial.animations } else { $true }
  notifications = [bool]$initial.notifications_enabled
}

New-Item -ItemType Directory -Force -Path $shotsDir | Out-Null
Get-ChildItem -LiteralPath $shotsDir -Filter "*.png" -ErrorAction SilentlyContinue | Remove-Item -Force
$null = Invoke-Qa -Arguments @("reset-logs", "show")

# -------------------------------------------------------------------- states
$states = @(
  @{ name = "classic-idle";        mode = "classic"; view = "chat";     demo = "empty" },
  @{ name = "classic-search";      mode = "classic"; view = "chat";     demo = "search" },
  @{ name = "classic-tools";       mode = "classic"; view = "chat";     demo = "tools" },
  @{ name = "classic-approval";    mode = "classic"; view = "chat";     demo = "approval" },
  @{ name = "classic-settings";    mode = "classic"; view = "settings"; demo = "none" },
  @{ name = "classic-memory";      mode = "classic"; view = "settings"; demo = "none"; category = "memory" },
  @{ name = "modern-idle";         mode = "modern";  view = "chat";     demo = "empty" },
  @{ name = "modern-reply";        mode = "modern";  view = "chat";     demo = "reply" },
  @{ name = "modern-search";       mode = "modern";  view = "chat";     demo = "search" },
  @{ name = "modern-tools";        mode = "modern";  view = "chat";     demo = "tools" },
  @{ name = "modern-approval";     mode = "modern";  view = "chat";     demo = "approval" },
  @{ name = "modern-settings";     mode = "modern";  view = "settings"; demo = "none" },
  @{ name = "modern-memory";       mode = "modern";  view = "settings"; demo = "none"; category = "memory" },
  @{ name = "modern-appearance";   mode = "modern";  view = "settings"; demo = "none"; category = "appearance" },
  @{ name = "modern-system";       mode = "modern";  view = "settings"; demo = "none"; category = "system" },
  @{ name = "modern-engine";       mode = "modern";  view = "settings"; demo = "none"; category = "engine" },
  @{ name = "modern-hardware";     mode = "modern";  view = "settings"; demo = "none"; category = "hardware" }
)

$audits = [System.Collections.ArrayList]::new()
$captures = [System.Collections.ArrayList]::new()

foreach ($state in $states) {
  $viewArg = if ($state.category) { "settings:{0}" -f $state.category } else { $state.view }
  $auditPane = if ($state.category) { $state.category } elseif ($state.view -eq "settings") { "general" } else { $state.view }
  $qaArgs = @(
    ("mode={0}" -f $state.mode),
    ("view={0}" -f $viewArg),
    ("demo={0}" -f $state.demo),
    ("backdrop=mica"),
    ("theme={0}" -f $Theme),
    "show",
    "audit"
  )
  $expectKind = "{0}:{1}:{2}:{3}" -f $state.mode, $Theme, $auditPane, $state.demo
  Write-Host ("State {0,-18} " -f $state.name) -NoNewline
  try {
    $null = Invoke-Qa -Arguments $qaArgs -Until {
      param($s)
      $s.layout_audit -and $s.layout_audit.kind -eq $expectKind
    } -TimeoutSeconds 20
  } catch {
    Add-Failure "$($state.name): state did not settle ($expectKind)"
    continue
  }

  $stateJson = Read-State
  $shotPath = Join-Path $shotsDir ("{0}.png" -f $state.name)
  $size = Capture-Window -Path $shotPath
  $audit = $stateJson.layout_audit
  $null = $audits.Add([pscustomobject]@{ state = $state.name; audit = $audit })
  $null = $captures.Add([pscustomobject]@{
      state = $state.name
      file = $shotPath
      width = $size.width
      height = $size.height
      viewport = $audit.viewport
    })
  Write-Host ("{0}x{1} -> {2}" -f $size.width, $size.height, (Split-Path -Leaf $shotPath)) -ForegroundColor DarkGray

  if ($audit.overflow.x -gt 0 -or $audit.overflow.y -gt 0) {
    Add-Failure ("{0}: content overflows the window (x={1}, y={2})" -f $state.name, $audit.overflow.x, $audit.overflow.y)
  } else {
    Add-Pass "$($state.name): no overflow"
  }
  if (@($audit.unlabeledButtons).Count -gt 0) {
    Add-Failure ("{0}: unlabeled controls: {1}" -f $state.name, (@($audit.unlabeledButtons) -join ", "))
  }
  foreach ($finding in @($audit.contrast)) {
    $delta = [double]$finding.required - [double]$finding.ratio
    if ($delta -gt 0.05) {
      $message = "{0}: contrast {1:0.00}:1 < {2}:1 for '{3}' ({4})" -f $state.name, $finding.ratio, $finding.required, $finding.text, $finding.selector
      if ([double]$finding.ratio -lt 3.0) {
        Add-Failure $message
      } else {
        Add-Warning $message
      }
    }
  }
  if (@($audit.smallTargets).Count -gt 0) {
    Add-Warning ("{0}: targets under 24px: {1}" -f $state.name, (($audit.smallTargets | ForEach-Object { "$($_.label) $($_.width)x$($_.height)" }) -join "; "))
  }
}

# ------------------------------------------------------ notifications capture
Write-Host "State notifications    " -NoNewline
try {
  $null = Invoke-Qa -Arguments @("mode=modern", "view=settings", "demo=none", "notifications=on", ("theme={0}" -f $Theme)) -Until {
    param($s)
    $s.notifications_status -eq "active" -and $s.layout_audit -and $s.layout_audit.kind -eq ("modern:{0}:general:none" -f $Theme)
  } -TimeoutSeconds 45
} catch {
  Add-Warning "notifications: Windows did not grant toast access in time"
}
try {
  # The panel refreshes hook state on a 3 s timer; capture the settled chip.
  Start-Sleep -Seconds 4
  $stateJson = Read-State
  $shotPath = Join-Path $shotsDir "modern-notifications.png"
  $size = Capture-Window -Path $shotPath
  $null = $captures.Add([pscustomobject]@{
      state = "modern-notifications"
      file = $shotPath
      width = $size.width
      height = $size.height
      viewport = $stateJson.layout_audit.viewport
    })
  $null = $audits.Add([pscustomobject]@{ state = "modern-notifications"; audit = $stateJson.layout_audit })
  Write-Host ("{0}x{1}" -f $size.width, $size.height) -ForegroundColor DarkGray
} finally {
  $null = Invoke-Qa -Arguments @("notifications=off") -Until { param($s) -not $s.notifications_enabled }
}

# ------------------------------------------------------------------ console
$stateAfter = Read-State
$consoleErrors = @($stateAfter.console_events | Where-Object { $_.level -eq "error" })
$consoleWarnings = @($stateAfter.console_events | Where-Object { $_.level -eq "warning" })
if ($consoleErrors.Count -gt 0) {
  foreach ($event in $consoleErrors) {
    Add-Failure ("console error: {0}" -f $event.message)
  }
} else {
  Add-Pass "no webview console errors"
}
if ($consoleWarnings.Count -gt 0) {
  foreach ($event in $consoleWarnings) {
    $text = [string]$event.message
    if ($text -match "^\[vite\]" -or $text -match "React DevTools") {
      continue
    }
    Add-Warning ("console warning: {0}" -f $text)
  }
}

# ---------------------------------------------------------------- animation
$animation = $null
if (-not $SkipAnimation) {
  $null = Invoke-Qa -Arguments @("mode=modern", "view=chat", "demo=empty") -Until {
    param($s)
    $s.layout_audit -and $s.layout_audit.kind -eq ("modern:{0}:chat:empty" -f $Theme)
  }
  Start-Sleep -Milliseconds 500

  function Wait-StableHeight {
    param([int]$TimeoutMs = 4000)
    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    $last = Get-WindowHeight
    $stableSince = Get-Date
    while ((Get-Date) -lt $deadline) {
      [System.Threading.Thread]::Sleep(40)
      $now = Get-WindowHeight
      if ([math]::Abs($now - $last) -le 1) {
        if (((Get-Date) - $stableSince).TotalMilliseconds -ge 260) { return $now }
      } else {
        $last = $now
        $stableSince = Get-Date
      }
    }
    return $last
  }

  function Measure-Transition {
    param([string[]]$TriggerArgs, [int]$ExpectSign)
    $start = Wait-StableHeight
    $samples = [System.Collections.ArrayList]::new()
    $proc = Start-Process -FilePath $script:appExe -ArgumentList (@("--qa") + $TriggerArgs) -PassThru
    $null = $proc.WaitForExit(5000)
    # 1 ms timer resolution so the 12 ms native steps are sampled individually.
    $null = [VisualQa.Native]::timeBeginPeriod(1)
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    try {
      while ($clock.ElapsedMilliseconds -lt 1100) {
        $null = $samples.Add(@{ t = $clock.ElapsedMilliseconds; h = (Get-WindowHeight) })
        [System.Threading.Thread]::Sleep(2)
      }
    } finally {
      $null = [VisualQa.Native]::timeEndPeriod(1)
    }
    $end = Wait-StableHeight
    $values = @($samples | ForEach-Object { $_.h })
    $first = -1
    $last = -1
    for ($i = 0; $i -lt $values.Count; $i++) {
      if ([math]::Abs($values[$i] - $start) -gt 2) { $first = $i; break }
    }
    for ($i = $values.Count - 1; $i -ge 0; $i--) {
      if ([math]::Abs($values[$i] - $end) -gt 2) { $last = $i; break }
    }
    if ($first -lt 0) { return $null }
    $deltas = @()
    for ($i = $first + 1; $i -le [math]::Min($last + 1, $values.Count - 1); $i++) {
      $deltas += ($values[$i] - $values[$i - 1])
    }
    return @{
      start = $start
      end = $end
      distinct = @($values | Select-Object -Unique).Count
      monotonic = -not ($deltas | Where-Object { ($_ * $ExpectSign) -lt -2 })
      maxStep = if ($deltas.Count) { ($deltas | ForEach-Object { [math]::Abs($_) } | Measure-Object -Maximum).Maximum } else { 0 }
      durationMs = if ($last -gt $first) { $samples[$last].t - $samples[$first].t } else { 0 }
      samples = @($values[$first..$last])
      direction = if ($end -gt $start) { "expand" } else { "collapse" }
    }
  }

  $preHeight = Wait-StableHeight
  if ($preHeight -gt 240) {
    Add-Failure ("animation: probe started at {0}px, expected the collapsed palette" -f $preHeight)
  }

  $expand = Measure-Transition -TriggerArgs @("demo=reply") -ExpectSign 1
  $collapse = Measure-Transition -TriggerArgs @("demo=empty") -ExpectSign -1
  $animation = @{ expand = $expand; collapse = $collapse }

  foreach ($pair in @(@{ label = "expand"; data = $expand; sign = 1 }, @{ label = "collapse"; data = $collapse; sign = -1 })) {
    $label = $pair.label
    $data = $pair.data
    if ($null -eq $data) {
      Add-Failure "animation: $label produced no height change"
      continue
    }
    if ($data.monotonic -eq $false) {
      Add-Failure ("animation: {0} is not monotonic ({1})" -f $label, ($data.samples -join ","))
    }
    if ($data.maxStep -gt 90) {
      Add-Failure ("animation: {0} jumps {1}px in one step" -f $label, $data.maxStep)
    }
    if ($data.distinct -lt 4) {
      Add-Warning ("animation: {0} only has {1} distinct heights (sampling or easing too fast)" -f $label, $data.distinct)
    }
    if ($data.durationMs -gt 700) {
      Add-Failure ("animation: {0} takes {1}ms" -f $label, $data.durationMs)
    }
    if ($data.monotonic -and $data.maxStep -le 90) {
      Add-Pass ("animation: {0} {1}ms, max step {2}px, {3} heights" -f $label, $data.durationMs, $data.maxStep, $data.distinct)
    }
  }
}

# -------------------------------------------------------------------- vision
$vision = $null
if (-not $SkipVision) {
  $visionJson = Join-Path $repo (Join-Path $OutDir "vision-report.json")
  if (Test-Path $visionJson) { Remove-Item -Force $visionJson }
  Write-Host ""
  $python = Join-Path $repo "backend\.venv\Scripts\python.exe"
  & $python (Join-Path $PSScriptRoot "visual_qa.py") --shots $shotsDir --out (Join-Path $repo $OutDir) --threshold $Threshold
  $visionExit = $LASTEXITCODE
  if (Test-Path $visionJson) {
    $vision = Get-Content -Raw -LiteralPath $visionJson | ConvertFrom-Json
    if ($vision.summary.failed -gt 0) {
      foreach ($result in $vision.results) {
        $verdict = $result.verdict
        $bad = $verdict.issues | Where-Object { $_.severity -in @("critical", "major") }
        foreach ($issue in $bad) {
          Add-Failure ("vision[{0}]: {1} - {2}" -f $result.shot, $issue.where, $issue.problem)
        }
      }
      Add-Failure ("vision: {0}/{1} screenshots did not pass" -f $vision.summary.failed, $vision.summary.total)
    } else {
      Add-Pass ("vision: all {0} screenshots passed" -f $vision.summary.total)
    }
  } elseif ($visionExit -ne 0) {
    Add-Failure "vision: evaluator did not produce a report"
  }
}

# ------------------------------------------------------------------ restore
$restoreArgs = @(
  ("mode={0}" -f $original.mode),
  ("backdrop={0}" -f $original.backdrop),
  "view=chat",
  "demo=none",
  ("notifications={0}" -f $original.notifications.ToString().ToLower()),
  ("theme={0}" -f $original.theme),
  ("animations={0}" -f $original.animations.ToString().ToLower())
)
$null = Invoke-Qa -Arguments $restoreArgs -Until {
  param($s)
  $s.mode -eq $original.mode -and -not $s.notifications_enabled
}

# ------------------------------------------------------------------ summary
$auditSummary = @()
foreach ($entry in $audits) {
  $auditSummary += [pscustomobject]@{
    state = $entry.state
    kind = $entry.audit.kind
    contrast = @($entry.audit.contrast).Count
    smallTargets = @($entry.audit.smallTargets).Count
    truncated = @($entry.audit.truncated).Count
    viewport = $entry.audit.viewport
  }
}

$report = [pscustomobject]@{
  generated_at = (Get-Date).ToString("o")
  exe = $script:appExe
  shots = @($captures)
  audits = $auditSummary
  console = @{ errors = $consoleErrors; warnings = $consoleWarnings }
  animation = $animation
  vision = if ($vision) { $vision.summary } else { $null }
  failures = @($failures)
  warnings = @($warnings)
}
New-Item -ItemType Directory -Force -Path (Join-Path $repo $OutDir) | Out-Null
$report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $reportPath -Encoding utf8

Write-Host ""
Write-Host ("{0} failures, {1} warnings -> {2}" -f $failures.Count, $warnings.Count, $reportPath) -ForegroundColor $(if ($failures.Count) { "Red" } else { "Green" })
if ($failures.Count -gt 0) { exit 1 }
exit 0
