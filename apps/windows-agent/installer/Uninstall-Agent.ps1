#Requires -RunAsAdministrator
param([string]$InstallDir = "$env:ProgramFiles\CS2FarmAgent", [switch]$RemoveData)
$ErrorActionPreference = 'Stop'
Stop-Service CS2FarmAgent -ErrorAction SilentlyContinue
& sc.exe delete CS2FarmAgent | Out-Null
Remove-Item $InstallDir -Recurse -Force
if ($RemoveData) { Remove-Item "$env:ProgramData\CS2FarmAgent" -Recurse -Force }
Write-Output 'Agent removed. Revoke its credential in the central panel.'
