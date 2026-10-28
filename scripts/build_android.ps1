# Build the Compa Android companion (debug APK).
[CmdletBinding()]
param(
  [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$android = Join-Path $repo "android"
$sdk = Join-Path $env:LOCALAPPDATA "Android\Sdk"
$studioJbr = "C:\Program Files\Android\Android Studio\jbr"

if (Test-Path $studioJbr) {
  $env:JAVA_HOME = $studioJbr
}
if (Test-Path $sdk) {
  $env:ANDROID_HOME = $sdk
  $env:ANDROID_SDK_ROOT = $sdk
}

Push-Location $android
try {
  if (-not $SkipTests) {
    & .\gradlew.bat testDebugUnitTest --no-daemon
    if ($LASTEXITCODE -ne 0) { throw "Android unit tests failed" }
  }
  & .\gradlew.bat assembleDebug --no-daemon
  if ($LASTEXITCODE -ne 0) { throw "Android build failed" }
  $apk = Join-Path $android "app\build\outputs\apk\debug\app-debug.apk"
  $out = Join-Path $repo "build\android"
  New-Item -ItemType Directory -Force -Path $out | Out-Null
  Copy-Item $apk (Join-Path $out "CompaBridge-debug.apk") -Force
  Write-Host "APK: $(Join-Path $out 'CompaBridge-debug.apk')" -ForegroundColor Green
} finally {
  Pop-Location
}
