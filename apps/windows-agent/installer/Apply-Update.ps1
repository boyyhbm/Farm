#Requires -RunAsAdministrator
param([Parameter(Mandatory=$true)][string]$Release, [string]$InstallDir = "$env:ProgramFiles\CS2FarmAgent")
$ErrorActionPreference = 'Stop'
$DataDir = Join-Path $env:ProgramData 'CS2FarmAgent'
$Release = (Resolve-Path $Release).Path
$AllowedRoot = (Resolve-Path "$DataDir\updates").Path + '\'
if (!$Release.StartsWith($AllowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Release must be in protected update staging directory' }
if (!(Test-Path "$Release\agent\main.py")) { throw 'Invalid release structure' }
$Current = Join-Path $InstallDir 'current'
$Previous = Join-Path $InstallDir 'previous'
Stop-Service CS2FarmAgent
try {
    if (Test-Path $Previous) { Remove-Item $Previous -Recurse -Force }
    Move-Item $Current $Previous
    Copy-Item $Release $Current -Recurse
    # Dependencies remain pinned by installed environment; release must target the same dependency ABI.
    Start-Service CS2FarmAgent
    (Get-Service CS2FarmAgent).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
    Start-Sleep -Seconds 10
    if ((Get-Service CS2FarmAgent).Status -ne 'Running') { throw 'Updated service failed its startup stability check' }
} catch {
    Stop-Service CS2FarmAgent -ErrorAction SilentlyContinue
    Remove-Item $Current -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $Previous) { Move-Item $Previous $Current }
    @{code="AGENT_UPDATE_FAILED"; checked_at=[DateTime]::UtcNow.ToString("o"); phase="activation_rolled_back"} | ConvertTo-Json | Set-Content "$DataDir\update-failure.json" -Encoding utf8
    Start-Service CS2FarmAgent
    throw
}
Remove-Item "$DataDir\update-failure.json" -ErrorAction SilentlyContinue
Write-Output 'Signed release activated; previous version retained for rollback.'
