# Allow the packaged Compa backend to accept LAN bridge connections.
# Run in an elevated PowerShell (Administrator).
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
  [string]$Python = "$PSScriptRoot\..\build\Personal Agent\backend-runtime\python.exe",
  [int]$Port = 8765
)

$resolved = (Resolve-Path $Python).Path
$name = "Compa LAN bridge (private)"
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule `
  -DisplayName $name `
  -Direction Inbound `
  -Program $resolved `
  -Protocol TCP `
  -LocalPort $Port `
  -Profile Private `
  -Action Allow | Out-Null
Write-Host "Allowed $resolved on private networks, TCP $Port" -ForegroundColor Green
