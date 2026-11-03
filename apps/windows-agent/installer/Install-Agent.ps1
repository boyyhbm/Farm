#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory=$true)][string]$ServerUrl,
    [string]$InstallDir = "$env:ProgramFiles\CS2FarmAgent",
    [string]$SteamPath = "",
    [string]$CS2Path = "",
    [switch]$AllowLocalhost
)
$ErrorActionPreference = 'Stop'
if (Get-Service CS2FarmAgent -ErrorAction SilentlyContinue) { throw 'Agent service already exists. Use signed updates or uninstall first.' }
$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Repository = (Resolve-Path (Join-Path $SourceRoot '..\..')).Path
$DataDir = Join-Path $env:ProgramData 'CS2FarmAgent'
New-Item -ItemType Directory -Force $InstallDir,$DataDir | Out-Null
& icacls $InstallDir /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' | Out-Null
& icacls $DataDir /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' | Out-Null
$Current = Join-Path $InstallDir 'current'
New-Item -ItemType Directory -Force $Current | Out-Null
Copy-Item "$SourceRoot\*" $Current -Recurse -Force
Copy-Item "$Repository\packages\gameplay-test-engine\gameplay_test_engine" $Current -Recurse -Force
& py -3.12 -m venv "$InstallDir\venv"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ is required' }
$Python = Join-Path $InstallDir 'venv\Scripts\python.exe'
& $Python -m pip install $Current
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
$config = @{
    server_url=$ServerUrl; data_dir=$DataDir; heartbeat_seconds=10; test_capacity=4;
    steam_path=$(if ($SteamPath) {$SteamPath} else {$null}); cs2_path=$(if ($CS2Path) {$CS2Path} else {$null});
    allow_insecure_localhost=[bool]$AllowLocalhost; update_public_key=$null; allowed_update_host=$null; log_level='INFO'
}
$config | ConvertTo-Json | Set-Content "$DataDir\config.json" -Encoding utf8
# Enroll as LocalSystem: DPAPI credential must be encrypted by the service identity.
# Protected stdin file is removed by the helper immediately after use; token is never a process argument.
$SecureToken = Read-Host 'One-time node enrollment token from panel' -AsSecureString
$Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
try { $PlainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr) }
[IO.File]::WriteAllText("$DataDir\enroll.once", $PlainToken)
$PlainToken = $null
$Action = New-ScheduledTaskAction -Execute $Python -Argument ('"{0}\installer\enroll_system.py" "{1}\config.json"' -f $Current,$DataDir) -WorkingDirectory $Current
$Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName 'CS2FarmAgentEnroll' -Action $Action -Principal $Principal -Force | Out-Null
try {
    Start-ScheduledTask 'CS2FarmAgentEnroll'
    $deadline = (Get-Date).AddSeconds(60)
    do { Start-Sleep -Milliseconds 500; $state = (Get-ScheduledTask 'CS2FarmAgentEnroll').State }
    while ($state -eq 'Running' -and (Get-Date) -lt $deadline)
    $result = Get-ScheduledTaskInfo 'CS2FarmAgentEnroll'
    if ($result.LastTaskResult -ne 0 -or !(Test-Path "$DataDir\credentials.dat")) { throw "Enrollment failed; see $DataDir\agent.log" }
} finally {
    Unregister-ScheduledTask 'CS2FarmAgentEnroll' -Confirm:$false
    Remove-Item "$DataDir\enroll.once" -ErrorAction SilentlyContinue
}
# pywin32 service host uses the venv interpreter and the current source directory.
Push-Location $Current
try {
    & $Python "$Current\agent\service.py" --startup auto install
    if ($LASTEXITCODE -ne 0) { throw 'Windows service install failed' }
} finally { Pop-Location }
& sc.exe failure CS2FarmAgent reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null
& sc.exe failureflag CS2FarmAgent 1 | Out-Null
Start-Service CS2FarmAgent
Write-Output "Agent installed. Logs: $DataDir\agent.log. Sign in to Windows and Steam manually before CS2 jobs."
