# Installed-product test. It is CI/developer tooling, never an end-user requirement.
#Requires -Version 5.1
param(
    [Parameter(Mandatory=$true)][string]$Artifacts,
    [Parameter(Mandatory=$true)][string]$Output
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
New-Item -ItemType Directory -Force $Output | Out-Null
$Report = [ordered]@{
    executed_at=(Get-Date).ToUniversalTime().ToString('o')
    os=[Environment]::OSVersion.VersionString
    is_clean_windows11=$false
    scope='Actual Windows CI installation and installed bundled-runtime checks; PATH isolation is not proof of a clean OS.'
    checks=@()
    unverified=@('clean Windows 11 installation','physical second computer connection','agent after real Windows reboot','SmartScreen user interaction','signed release reputation','upgrade from a distinct previously released schema version')
}
function Record([string]$Name,[bool]$Passed,[string]$Details='') {
    $Report.checks += @{name=$Name; passed=$Passed; details=$Details}
    if (!$Passed) { throw "$Name failed: $Details" }
}
function Wait-ProcessResult([string]$Program,[string[]]$Arguments,[int]$Seconds=240) {
    $Process = Start-Process -FilePath $Program -ArgumentList $Arguments -PassThru
    if (!$Process.WaitForExit($Seconds * 1000)) {
        $Process.Kill()
        throw "Timed out waiting for $Program"
    }
    if ($Process.ExitCode -ne 0) { throw "$Program exited $($Process.ExitCode)" }
}
function Find-BundledRuntime([string]$InstallDir) {
    $RuntimeDir = Join-Path $InstallDir 'resources\runtime\farm-runtime'
    $Frozen = Join-Path $RuntimeDir 'farm-runtime.exe'
    if (Test-Path $Frozen) { return @{executable=$Frozen; arguments=@(); layout='frozen'} }
    $Python = Join-Path $RuntimeDir 'python.exe'
    $Entry = Join-Path $RuntimeDir 'desktop_entry.py'
    if ((Test-Path $Python) -and (Test-Path $Entry)) {
        return @{executable=$Python; arguments=@(([char]34 + $Entry + [char]34)); layout='embedded'}
    }
    throw 'The installed package contains no supported bundled Windows Python runtime.'
}
function Stop-Installed([string]$InstallDir) {
    # Stop only processes from this test installation, not arbitrary farm/user applications.
    Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir,[StringComparison]::OrdinalIgnoreCase) } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
try {
    $InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\CS2FarmInstallerTest'
    foreach ($Product in @('manager','agent')) {
        $SetupName = if ($Product -eq 'manager') {'CS2-Farm-Manager-Setup.exe'} else {'CS2-Farm-Agent-Setup.exe'}
        $ExeName = if ($Product -eq 'manager') {'CS2-Farm-Manager.exe'} else {'CS2-Farm-Agent.exe'}
        $DisplayName = if ($Product -eq 'manager') {'CS2 Farm Manager'} else {'CS2 Farm Agent'}
        $InstallDir = Join-Path $InstallRoot $Product
        $Setup = Join-Path $Artifacts "$Product\$SetupName"
        Wait-ProcessResult $Setup @('/S',"/D=$InstallDir")
        $Exe = Join-Path $InstallDir $ExeName
        Record "${Product}_installed_executable" (Test-Path $Exe) $ExeName
        $BundledRuntime = Find-BundledRuntime $InstallDir
        Record "${Product}_python_runtime_bundled" $true $BundledRuntime.layout
        Record "${Product}_start_menu_shortcut" (Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\$DisplayName.lnk")
        $Desktop = [Environment]::GetFolderPath('Desktop')
        Record "${Product}_desktop_shortcut" (Test-Path (Join-Path $Desktop "$DisplayName.lnk"))
        Stop-Installed $InstallDir
        if ($Product -eq 'manager') {
            Record 'node_runtime_bundled' (Test-Path "$InstallDir\resources\node\node.exe")
            Record 'dashboard_bundled' (Test-Path "$InstallDir\resources\dashboard\server.js")
            Record 'agent_installer_available_offline' (Test-Path "$InstallDir\resources\downloads\CS2-Farm-Agent-Setup.exe")
            $OriginalPath = $env:PATH
            # Give child processes only standard Windows paths. No system Python, Node.js or Docker.
            $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem"
            try {
                $Result = Join-Path $Output 'manager-smoke.json'
                Wait-ProcessResult $Exe @('--smoke-test','--result',"`"$Result`"") 360
                if (!(Test-Path $Result)) { throw 'Packaged app did not write smoke result.' }
                $Smoke = Get-Content $Result -Raw | ConvertFrom-Json
                Record 'installed_manager_smoke' ([bool]$Smoke.passed) (Get-Content $Result -Raw)
                $RuntimePrefix = @($BundledRuntime.arguments | ForEach-Object { $_.Trim([char]34) })
                $Before = & $BundledRuntime.executable @RuntimePrefix data-check --data-dir $Smoke.dataDirectory
                if ($LASTEXITCODE -ne 0) { throw 'Pre-upgrade database verification failed.' }
                # The same release exercises installer replacement and preservation.
                # It does not establish compatibility of an untested future schema migration.
                Wait-ProcessResult $Setup @('/S',"/D=$InstallDir")
                $After = & $BundledRuntime.executable @RuntimePrefix data-check --data-dir $Smoke.dataDirectory
                if ($LASTEXITCODE -ne 0) { throw 'Post-upgrade database verification failed.' }
                Record 'repair_install_preserves_all_database_rows' (($Before -join '') -eq ($After -join '')) 'Read-only canonical SHA-256 for every table in both SQLite databases compared before and after real installer replacement.'
                $Resume = Join-Path $Output 'manager-resume.json'
                Wait-ProcessResult $Exe @('--smoke-test','--smoke-resume','--smoke-data-dir',"`"$($Smoke.dataDirectory)`"",'--result',"`"$Resume`"") 240
                $Resumed = Get-Content $Resume -Raw | ConvertFrom-Json
                Record 'manager_relaunches_persisted_database_after_reinstall' ([bool]$Resumed.passed) (Get-Content $Resume -Raw)
            } finally { $env:PATH = $OriginalPath }
        } else {
            # GUI must open from the installed Agent product without developer runtimes.
            $RuntimeArguments = @($BundledRuntime.arguments) + @('--help')
            Wait-ProcessResult $BundledRuntime.executable $RuntimeArguments 30
            $env:FARM_INSTALLED_RUNTIME_DIR = Join-Path $InstallDir 'resources\runtime\farm-runtime'
            try {
                & python -m pytest tests/integration/test_windows_services.py -q --tb=short "--junitxml=$Output/windows-services.xml"
                if ($LASTEXITCODE -ne 0) { throw 'Actual installed Windows SCM/two-agent integration failed.' }
                Record 'two_real_scm_agents' $true 'Two LocalSystem services on one Windows runner: TLS, telemetry, parallel jobs, stop, rotation, offline UNKNOWN and reconnect. Physical hosts and OS reboot remain unverified.'
            } finally { Remove-Item Env:FARM_INSTALLED_RUNTIME_DIR -ErrorAction SilentlyContinue }
            Record 'installed_agent_runtime_starts' $true 'Native bundled runtime --help exited successfully; enrollment and reboot not inferred.'
        }
        Stop-Installed $InstallDir
        $Registration = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' |
            Where-Object { $_.PSObject.Properties['DisplayName'] -and $_.DisplayName -eq $DisplayName } | Select-Object -First 1
        Record "${Product}_registered_in_windows_settings" ([bool]$Registration)
        $Uninstaller = Get-ChildItem $InstallDir -Filter '*Uninstall*.exe' | Select-Object -First 1
        Record "${Product}_uninstaller_present" ([bool]$Uninstaller)
        Wait-ProcessResult $Uninstaller.FullName @('/S') 120
        $RemoveDeadline = (Get-Date).AddSeconds(30)
        while ((Test-Path $Exe) -and (Get-Date) -lt $RemoveDeadline) { Start-Sleep -Milliseconds 500 }
        Record "${Product}_program_removed" (!(Test-Path $Exe))
    }
    $Report.passed = $true
} catch {
    $Report.passed = $false
    $Report.error = $_.Exception.Message
    throw
} finally {
    [IO.File]::WriteAllText((Join-Path $Output 'installer-smoke.json'), ($Report | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
}
