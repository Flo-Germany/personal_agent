<#
.SYNOPSIS
    Build the click-to-run Windows app (frontend + backend in one folder).

.DESCRIPTION
    The desktop shell is a Tauri app. The Python backend is shipped with a
    private embeddable CPython runtime and the exact site-packages of
    backend\.venv, so the target machine does not need Python, Node.js, or a
    terminal.

    Output (default):
        build\Personal Agent\Personal Agent.exe
        build\Personal Agent\backend\
        build\Personal Agent\backend-runtime\
        build\Personal Agent\config\

    Double-click "Personal Agent.exe" to start the UI and the backend. The
    backend runs without a console window.

.PARAMETER Installer
    Also build the NSIS installer (adds several minutes and a large .exe).

.PARAMETER Zip
    Also create build\Personal Agent.zip for copying to another PC.

.PARAMETER SkipFrontendInstall
    Skip "npm install" when node_modules already exists and is up to date.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -Zip -Installer
#>
[CmdletBinding()]
param(
    [switch]$Installer,
    [switch]$Zip,
    [switch]$SkipFrontendInstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProgressPreference = "SilentlyContinue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $scriptDir
$backendDir = Join-Path $repo "backend"
$frontendDir = Join-Path $repo "frontend"
$tauriDir = Join-Path $frontendDir "src-tauri"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"

$buildDir = Join-Path $repo "build"
$cacheDir = Join-Path $buildDir "cache"
$stageDir = Join-Path $buildDir "bundle"
$runtimeDir = Join-Path $stageDir "runtime"
$sitePackages = Join-Path $runtimeDir "Lib\site-packages"
$portableDir = Join-Path $buildDir "Personal Agent"
$portableExe = Join-Path $portableDir "Personal Agent.exe"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Robocopy {
    param(
        [Parameter(Mandatory)] [string]$Source,
        [Parameter(Mandatory)] [string]$Destination,
        [string[]]$ExcludeDirs = @(),
        [string[]]$ExcludeFiles = @()
    )

    $roboArgs = @(
        $Source, $Destination,
        "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1"
    )
    if ($ExcludeDirs.Count -gt 0) {
        $roboArgs += "/XD"
        $roboArgs += $ExcludeDirs
    }
    if ($ExcludeFiles.Count -gt 0) {
        $roboArgs += "/XF"
        $roboArgs += $ExcludeFiles
    }

    & robocopy.exe @roboArgs | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed with exit code $LASTEXITCODE ($Source -> $Destination)"
    }
}

function Assert-Tool([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name was not found. $Hint"
    }
}

# ---------------------------------------------------------------- preflight
Write-Step "Checking build prerequisites"
if (-not (Test-Path $venvPython)) {
    throw "Backend virtualenv not found at $venvPython. Create it with: python -m venv backend\.venv; backend\.venv\Scripts\pip install -r backend\requirements.txt"
}
Assert-Tool "npm" "Install Node.js 20 or newer."
Assert-Tool "cargo" "Install the Rust toolchain (rustup) and the VS C++ build tools."
Assert-Tool "robocopy" "robocopy is part of Windows."
if (-not (Test-Path (Join-Path $frontendDir "node_modules")) -and -not $SkipFrontendInstall) {
    Write-Host "node_modules is missing; running npm install."
    Push-Location $frontendDir
    try { & npm install; if ($LASTEXITCODE -ne 0) { throw "npm install failed." } }
    finally { Pop-Location }
}

$pythonVersion = (& $venvPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
$pythonTag = & $venvPython -c "import sys; print(f'python{sys.version_info[0]}{sys.version_info[1]}')"
$architecture = (& $venvPython -c "import platform; print(platform.machine().lower())").Trim()
$embedArch = if ($architecture -match "arm") { "arm64" } else { "amd64" }
Write-Host "Backend Python: $pythonVersion ($embedArch)"

# ------------------------------------------------------- embeddable Python
Write-Step "Preparing the embeddable Python $pythonVersion runtime"
New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
$embedZip = Join-Path $cacheDir "python-$pythonVersion-embed-$embedArch.zip"
if (-not (Test-Path $embedZip)) {
    $url = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-$embedArch.zip"
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $embedZip -UseBasicParsing
}
if ((Get-Item $embedZip).Length -lt 1MB) {
    throw "The embeddable Python archive looks invalid: $embedZip"
}

if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
Expand-Archive -Path $embedZip -DestinationPath $runtimeDir -Force

# Enable the standard library layout and site-packages. A _pth file replaces
# normal path discovery, so "import site" must be enabled explicitly.
$pthFile = Get-ChildItem -Path $runtimeDir -Filter "python*._pth" | Select-Object -First 1
if (-not $pthFile) { throw "The embeddable distribution has no _pth file." }
$zipName = $pthFile.Name -replace "\._pth$", ".zip"
@(
    $zipName
    "."
    "Lib\site-packages"
    "import site"
) | Set-Content -Path $pthFile.FullName -Encoding Ascii

# ------------------------------------------------ backend + site-packages
Write-Step "Copying backend dependencies (this can take a few minutes)"
New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
Invoke-Robocopy -Source (Join-Path $backendDir ".venv\Lib\site-packages") -Destination $sitePackages

Write-Step "Copying backend sources"
New-Item -ItemType Directory -Force -Path (Join-Path $stageDir "backend") | Out-Null
Invoke-Robocopy `
    -Source $backendDir `
    -Destination (Join-Path $stageDir "backend") `
    -ExcludeDirs @(".venv", "tests", "__pycache__", ".pytest_cache") `
    -ExcludeFiles @("*.pyc", "*.pyo")

Write-Step "Copying default configuration"
New-Item -ItemType Directory -Force -Path (Join-Path $stageDir "config") | Out-Null
Invoke-Robocopy -Source (Join-Path $repo "config") -Destination (Join-Path $stageDir "config")

# ---------------------------------------------------------- runtime smoke test
Write-Step "Verifying the bundled runtime"
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONUTF8 = "1"
$importCheck = "import fastapi, uvicorn, numpy, PIL, cv2, psutil, matplotlib; import pywinauto, mss, pynput, rapidocr_onnxruntime; import pyvda, comtypes; import torch, sentence_transformers; print('imports ok')"
& (Join-Path $runtimeDir "python.exe") -c $importCheck
if ($LASTEXITCODE -ne 0) {
    throw "The bundled Python runtime cannot import the backend dependencies."
}

$smokeData = Join-Path $buildDir "smoke-data"
if (Test-Path $smokeData) { Remove-Item -Recurse -Force $smokeData }
$env:PERSONAL_AGENT_DATA_DIR = $smokeData
$smokeOut = Join-Path $buildDir "smoke.out.log"
$smokeErr = Join-Path $buildDir "smoke.err.log"
$smoke = Start-Process `
    -FilePath (Join-Path $runtimeDir "python.exe") `
    -ArgumentList @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8010") `
    -WorkingDirectory (Join-Path $stageDir "backend") `
    -RedirectStandardOutput $smokeOut `
    -RedirectStandardError $smokeErr `
    -WindowStyle Hidden `
    -PassThru

try {
    $ready = $false
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:8010/health" -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 400
    }
    if (-not $ready) {
        Get-Content $smokeErr -Tail 40 -ErrorAction SilentlyContinue | Write-Host
        throw "The bundled backend did not answer /health on port 8010."
    }
    $identity = Invoke-RestMethod -Uri "http://127.0.0.1:8010/identity" -TimeoutSec 10
    Write-Host "Bundled backend OK: $($identity.name) / $($identity.model)"
} finally {
    if ($smoke -and -not $smoke.HasExited) {
        & taskkill.exe /PID $smoke.Id /T /F | Out-Null
    }
    Remove-Item Env:\PERSONAL_AGENT_DATA_DIR -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------- frontend build
Write-Step "Building the desktop app"

# The backend payload is supplied as a build-time config override instead of
# living in tauri.conf.json. This keeps `tauri dev` working on a fresh checkout,
# where no staged runtime exists yet.
$bundleConfig = Join-Path $buildDir "bundle-config.json"
@{
    bundle = @{
        resources = @{
            (Join-Path $stageDir "backend") = "backend"
            (Join-Path $stageDir "runtime") = "backend-runtime"
            (Join-Path $stageDir "config") = "config"
        }
    }
} | ConvertTo-Json -Depth 6 | Set-Content -Path $bundleConfig -Encoding Ascii

Push-Location $frontendDir
try {
    if ($Installer) {
        & npm run tauri build -- --config $bundleConfig
    } else {
        & npm run tauri build -- --config $bundleConfig --no-bundle
    }
    if ($LASTEXITCODE -ne 0) { throw "The Tauri build failed." }
}
finally { Pop-Location }

$builtExe = Join-Path $tauriDir "target\release\personal-agent.exe"
if (-not (Test-Path $builtExe)) { throw "Expected build output not found: $builtExe" }

# ------------------------------------------------------------ portable folder
Write-Step "Assembling the portable app"
if (Test-Path $portableDir) {
    # OneDrive can hold a handle on the folder root (cloud placeholders), so
    # deleting the root itself fails even when it is empty. Clear the contents
    # instead; the folder name stays stable for shortcuts and the launcher.
    Get-ChildItem -LiteralPath $portableDir -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $leftover = Get-ChildItem -LiteralPath $portableDir -Force -ErrorAction SilentlyContinue
    if ($leftover) {
        throw "Could not clear $portableDir. Close the app and any Explorer window on it, then retry."
    }
}
New-Item -ItemType Directory -Force -Path $portableDir | Out-Null
Copy-Item $builtExe $portableExe
Invoke-Robocopy -Source (Join-Path $stageDir "backend") -Destination (Join-Path $portableDir "backend")
Invoke-Robocopy -Source $runtimeDir -Destination (Join-Path $portableDir "backend-runtime")
Invoke-Robocopy -Source (Join-Path $stageDir "config") -Destination (Join-Path $portableDir "config")

$readme = Join-Path $portableDir "Read me.txt"
@"
Personal Agent
==============

Double-click "Personal Agent.exe" to start.

The window opens right away while the local backend and Ollama start in the
background. The app lives in the system tray; closing the window hides it.
Use the tray icon to show it again or choose Quit.

Requirements
------------
- Windows 10/11 (64-bit)
- Ollama with the configured model:
    ollama pull qwen3-vl:8b-instruct

Data (chat knowledge base, screenshots, config):
  %LOCALAPPDATA%\com.personalagent.desktop\data
Logs:
  %LOCALAPPDATA%\com.personalagent.desktop\logs

Keep this folder together. Moving only the .exe will break the backend.
"@ | Set-Content -Path $readme -Encoding UTF8

if ($Zip) {
    Write-Step "Creating the ZIP archive"
    $zipPath = Join-Path $buildDir "Personal Agent.zip"
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $portableDir,
        $zipPath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $true
    )
    Write-Host "ZIP: $zipPath"
}

$sizeMb = [math]::Round(((Get-ChildItem $portableDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 0)
Write-Step "Done"
Write-Host "App folder: $portableDir ($sizeMb MB)"
Write-Host "Executable: $portableExe"
if ($Installer) {
    Write-Host "Installer:  $(Join-Path $tauriDir 'target\release\bundle\nsis')"
}
Write-Host ""
Write-Host "Double-click 'Personal Agent.exe' to start the app."
