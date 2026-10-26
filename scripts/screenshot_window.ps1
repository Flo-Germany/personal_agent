<#
.SYNOPSIS
  Capture the Compa window (including Mica / OLED compositing) to a PNG.

.DESCRIPTION
  Used for visual QA of the classic and modern shells. The capture is a normal
  desktop grab, so DWM effects (Mica, rounded corners, transparency) appear
  exactly as the user sees them.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\screenshot_window.ps1 -Out build\modern.png
#>
[CmdletBinding()]
param(
  [string]$Title = "Personal Agent",
  [string]$Out = "build\screenshot.png",
  [int]$DelayMs = 700
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing

if (-not ("ShotNative" -as [type])) {
  Add-Type -Namespace Shot -Name Native -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern System.IntPtr FindWindow(string className, string windowName);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool GetWindowRect(System.IntPtr hWnd, out RECT rect);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool ShowWindow(System.IntPtr hWnd, int command);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SetProcessDPIAware();
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool IsWindowVisible(System.IntPtr hWnd);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SetWindowPos(System.IntPtr hWnd, System.IntPtr after, int x, int y, int cx, int cy, uint flags);
[System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
"@
}

$null = [Shot.Native]::SetProcessDPIAware()
$handle = [Shot.Native]::FindWindow([NullString]::Value, $Title)
if ($handle -eq [IntPtr]::Zero) {
  throw "Window '$Title' not found. Is the app running?"
}

if (-not [Shot.Native]::IsWindowVisible($handle)) {
  $null = [Shot.Native]::ShowWindow($handle, 9)
}
# Raise above other windows only for the capture; the app stays where it was.
$HWND_TOPMOST = [IntPtr](-1)
$SWP_FLAGS = 0x0001 -bor 0x0002 -bor 0x0040
$null = [Shot.Native]::SetWindowPos($handle, $HWND_TOPMOST, 0, 0, 0, 0, $SWP_FLAGS)
$null = [Shot.Native]::SetForegroundWindow($handle)
Start-Sleep -Milliseconds $DelayMs

$rect = New-Object Shot.Native+RECT
if (-not [Shot.Native]::GetWindowRect($handle, [ref]$rect)) {
  throw "GetWindowRect failed"
}
$width = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top
if ($width -le 0 -or $height -le 0) {
  throw "Window has no drawable area ($width x $height)"
}

$bitmap = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {
  $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
} finally {
  $graphics.Dispose()
}

$outPath = Join-Path (Get-Location) $Out
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outPath) | Out-Null
$bitmap.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
$bitmap.Dispose()
$null = [Shot.Native]::SetWindowPos($handle, [IntPtr](-2), 0, 0, 0, 0, $SWP_FLAGS)

Write-Host ("Captured {0}x{1} -> {2}" -f $width, $height, $outPath) -ForegroundColor Green
