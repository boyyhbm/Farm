# Build tooling only. End users use the graphical EXE installer and never run this script.
#Requires -Version 5.1
param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root
if ($env:OS -ne 'Windows_NT') { throw 'The native Python runtime must be built on Windows x64.' }
$Stage = Join-Path $Root 'build\windows\stage'
$Out = Join-Path $Root 'artifacts\windows'
New-Item -ItemType Directory -Force $Stage,$Out | Out-Null
function Run-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}
if (!$SkipInstall) {
    Run-Checked 'python' @('-m','pip','install','--require-hashes','-r','build/windows/requirements.lock')
    Run-Checked 'npm' @('ci','--prefix','apps/dashboard')
    Run-Checked 'npm' @('ci','--prefix','apps/desktop')
}
$env:API_URL = 'http://127.0.0.1:17831'
$env:NEXT_TELEMETRY_DISABLED = '1'
Run-Checked 'npm' @('run','typecheck','--prefix','apps/dashboard')
Run-Checked 'npm' @('run','build','--prefix','apps/dashboard')
$DashboardSource = Join-Path $Root 'apps\dashboard\.next\standalone'
if (!(Test-Path (Join-Path $DashboardSource 'server.js'))) { throw 'Next standalone server.js was not generated.' }
$DashboardStage = Join-Path $Stage 'dashboard'
if (Test-Path $DashboardStage) { Remove-Item $DashboardStage -Recurse -Force }
Copy-Item $DashboardSource $DashboardStage -Recurse
# Node runtime is independently bundled; no system Node.js is needed after installation.
$NodeManifest = Get-Content 'build/windows/node-runtime.json' -Raw | ConvertFrom-Json
$NodeArchive = Join-Path $Stage 'node-download.zip'
$NodeUnpack = Join-Path $Stage 'node-download'
$NodeStage = Join-Path $Stage 'node'
Invoke-WebRequest -Uri $NodeManifest.url -OutFile $NodeArchive -UseBasicParsing
if ((Get-FileHash $NodeArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $NodeManifest.sha256) {
    Remove-Item $NodeArchive -Force
    throw 'Official Node.js archive SHA256 mismatch; refusing to package it.'
}
if (Test-Path $NodeUnpack) { Remove-Item $NodeUnpack -Recurse -Force }
Expand-Archive $NodeArchive $NodeUnpack -Force
$NodeDirectory = Get-ChildItem $NodeUnpack -Directory | Select-Object -First 1
New-Item -ItemType Directory -Force $NodeStage | Out-Null
Copy-Item (Join-Path $NodeDirectory.FullName 'node.exe') $NodeStage -Force
Copy-Item (Join-Path $NodeDirectory.FullName 'LICENSE') $NodeStage -Force
Remove-Item $NodeArchive -Force
Remove-Item $NodeUnpack -Recurse -Force
Run-Checked 'python' @('-m','PyInstaller','--noconfirm','--clean','--distpath',"$Stage\runtime",'--workpath',"$Stage\pyinstaller-work",'build/windows/runtime.spec')
$RuntimeExe = Join-Path $Stage 'runtime\farm-runtime\farm-runtime.exe'
if (!(Test-Path $RuntimeExe)) { throw 'The native bundled Python runtime was not generated.' }
Run-Checked $RuntimeExe @('--help')
$DesktopVersion = (Get-Content 'apps/desktop/package.json' -Raw | ConvertFrom-Json).version
$Builder = Join-Path $Root 'apps\desktop\node_modules\.bin\electron-builder.cmd'
$ProductFile = Join-Path $Stage 'product.json'
$Downloads = Join-Path $Stage 'downloads'
New-Item -ItemType Directory -Force $Downloads | Out-Null
foreach ($Product in @('agent','manager')) {
    $env:FARM_PRODUCT = $Product
    $DefaultMode = if ($Product -eq 'agent') {'agent'} else {'server'}
    $Name = if ($Product -eq 'agent') {'CS2 Farm Agent'} else {'CS2 Farm Manager'}
    [IO.File]::WriteAllText($ProductFile, (@{ defaultMode=$DefaultMode; productName=$Name; version=$DesktopVersion; agentOnly=($Product -eq 'agent') } | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    Run-Checked $Builder @('--win','nsis','--x64','--config','build/windows/electron-builder.cjs','--publish','never')
    if ($Product -eq 'agent') {
        $AgentSetup = Join-Path $Out 'agent\CS2-Farm-Agent-Setup.exe'
        if (!(Test-Path $AgentSetup)) { throw 'Agent setup was not generated.' }
        Copy-Item $AgentSetup $Downloads -Force
        [IO.File]::WriteAllText("$Downloads\agent-manifest.json", (@{ filename='CS2-Farm-Agent-Setup.exe'; version=$DesktopVersion; sha256=(Get-FileHash $AgentSetup -Algorithm SHA256).Hash.ToLowerInvariant() } | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    }
}
$Checksums = @{}
foreach ($Product in @('agent','manager')) {
    $Filename = if ($Product -eq 'agent') {'CS2-Farm-Agent-Setup.exe'} else {'CS2-Farm-Manager-Setup.exe'}
    $File = Join-Path $Out "$Product\$Filename"
    if (!(Test-Path $File)) { throw "Missing final artifact $Filename" }
    $Signature = Get-AuthenticodeSignature $File
    if ($env:CSC_LINK -and $Signature.Status -ne 'Valid') { throw "Configured signing did not produce a valid Authenticode signature for $Filename" }
    $Checksums[$Filename] = @{sha256=(Get-FileHash $File -Algorithm SHA256).Hash.ToLowerInvariant(); authenticode=$Signature.Status.ToString(); size_bytes=(Get-Item $File).Length}
}
[IO.File]::WriteAllText("$Out\build-manifest.json", (@{generated_at=(Get-Date).ToUniversalTime().ToString('o'); artifacts=$Checksums; node=$NodeManifest; warning='Packaging success alone does not establish installation or reboot success.'} | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
Write-Output 'Native Windows installers created. Run the installed-application smoke workflow before distribution.'
