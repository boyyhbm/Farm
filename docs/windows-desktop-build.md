# Windows Desktop: native build and verified artifacts

The end-user products are **CS2-Farm-Manager-Setup.exe** and **CS2-Farm-Agent-Setup.exe**. They are assisted graphical NSIS installers, with desktop/Start-menu shortcuts, Windows Settings uninstall registration and a bundled Electron/Python runtime. The Manager also contains the existing compiled Next.js dashboard and official Node.js binary. End users do not install Python, Node.js, PostgreSQL, Redis or Docker and do not execute build commands.

The source repository itself is not an installer. Do not rename a ZIP or Linux binary to `.exe`. The build only reports success after native Windows binaries and both actual NSIS artifacts exist.

## Automated native Windows build

The workflow is `.github/workflows/windows-desktop.yml`. On a GitHub repository containing this code, **Actions → Windows desktop installers → Run workflow** builds on Windows Server 2022 x64. The runtime choice is `embedded` by default (bundled actual Windows CPython) or `frozen` (PyInstaller). A `desktop-v*` tag also triggers it. The build requires no application secrets. For the native `frozen` variant, optional Authenticode signing uses repository secrets `WINDOWS_CSC_LINK` and `WINDOWS_CSC_KEY_PASSWORD`. The portable `embedded` variant intentionally emits unsigned installers and rejects configured signing secrets; choose `frozen` when signing is required.

The uploaded `CS2-Farm-Windows-x64` artifact contains:

- `CS2-Farm-Manager-Setup.exe`;
- `CS2-Farm-Agent-Setup.exe`;
- `build-manifest.json`, including SHA256, file sizes and observed signing status;
- `verification/installer-smoke.json` and the actual desktop smoke report when tests execute.

A green compilation step alone is not proof that installation or runtime tests passed. Inspect the test steps and generated JSON. A workflow uploaded but not executed provides no Windows verification result.

The repository is connected to [boyyhbm/Farm](https://github.com/boyyhbm/Farm). Windows CI was dispatched during validation; actual run results are recorded in [windows-desktop-verification.md](windows-desktop-verification.md). Packaging and installation are separate checks. No step is considered passed merely because its workflow file exists.

## Bundling and component provenance

The embedded layout is `resources/runtime/farm-runtime/python.exe` with `desktop_entry.py`, the application source, migrations and Windows native Python packages beside the runtime. The Python binary is shipped inside the installer; the target PC does not install Python. The supervisor explicitly locates this binary and passes the fixed entry-point prefix. It prefers the frozen `farm-runtime.exe` layout when present, so both build modes keep the same application behavior. `scripts/build-windows-cross.py` is the portable maintainer entry point; `uv` is a build dependency, not an end-user dependency.

The no-Wine NSIS template is `build/windows/portable-installer.nsi`: it embeds the real unpacked Windows application and writes a real uninstaller during installation. This avoids electron-builder's default Linux uninstaller extraction step, which otherwise executes an intermediate EXE through Wine. Turning off executable resource editing alone would not avoid that step. Linux cross-built installers remain unsigned and must be labeled as Windows-untested; the native frozen Windows packaging may use the configured signing certificate.

`build/windows/runtime.spec` freezes `apps/api/desktop_entry.py` with application modules, Alembic migrations, Windows native extensions and the isolated test engine. It uses a console-capable runtime so the Electron supervisor can communicate through stdin/stdout; the supervisor starts it with `windowsHide: true`, so no persistent console is shown. The executable name is fixed; arbitrary remote executables are never accepted.

`build/windows/node-runtime.json` pins Node.js 24.22.1 win-x64 and its exact official SHA256. The Windows build downloads through normal verified HTTPS and refuses mismatched bytes. Neither TLS verification nor SmartScreen is disabled. Electron and electron-builder versions are pinned by the desktop package lock. Backend dependencies use `build/windows/requirements.lock`, generated explicitly for Windows x64/Python 3.12; this includes pywin32, pinned PyInstaller/uv and their transitive build dependencies, and excludes Linux-only uvloop. The existing Linux server lock remains unchanged.

The build creates Agent first, then bundles that actual Agent installer into Manager's `resources/downloads/`. The main panel can serve that exact file. Both products have distinct application IDs and uninstall entries; removing Agent does not remove Manager's installation.

## What CI tests and what it does not

Before packaging, CI runs the Python unit suite and the separate SQLite/local-runtime integration suite without Redis/PostgreSQL. That suite includes actual source-runtime startup, administrator/database creation, persistence, backup/restore and TLS agent connection. Those results are written to JUnit XML when CI executes; no CI execution has been claimed here.

The installed-application smoke script performs a real NSIS installation, checks bundled runtime files and shortcuts, launches the installed Manager with a PATH containing only standard Windows locations, and invokes the shell's packaged smoke mode. That mode is expected to create the local database/admin account, run API/dashboard, and prove data persists after supervisor restart. The script checks Windows Settings uninstall registration and actually runs each uninstaller.

The runner is **Windows Server 2022, not a clean physical Windows 11 installation**. Removing developer runtimes from PATH demonstrates that the packaged application uses its own runtimes; it does not prove the OS never had those runtimes installed. Physical second-PC pairing, actual reboot persistence, interactive Steam/CS2 desktop behavior, clean Windows 11 installation, SmartScreen prompts and code-signing reputation remain separate tests. They must remain explicitly unverified until run on that hardware.

Unsigned installers may prompt SmartScreen or be blocked by local policy. The project does not disable those protections. Commercial distribution should use an Authenticode certificate and a trusted timestamp service; a valid signature does not immediately guarantee SmartScreen reputation. The build fails if signing is configured but resulting signatures are invalid.

## Reproducible developer entry point

The frozen CI entry point is `scripts/build-windows.ps1` and the embedded entry point is `scripts/build-windows-cross.py`; the installed-product test is `build/windows/smoke-installed.ps1`. These are maintainer tooling, not part of the user's setup flow. Application data is preserved during an ordinary silent uninstall; interactive uninstall explicitly asks before irreversible local data deletion. No firewall, antivirus exclusion or trusted certificate is installed automatically.

Agent setup offers an actual LocalSystem SCM service through UAC, with separately enrolled SYSTEM-bound DPAPI credentials and a protected Program Files runtime. It continues independently of the GUI. The interactive after-login mode remains available. Steam/CS2 always need an active console user. Service runtime replacement, rollback and explicit re-enrollment are managed through the GUI. Actual reboot behavior requires the physical acceptance checklist.

## Artifacts actually produced in this session

Both genuine unsigned NSIS installers were cross-compiled successfully on Linux. Their full container CRC, declared size and PE structure pass verification; hashes and exact byte counts are recorded in `docs/windows-artifact-verification.json`. Current Windows execution results are recorded in the verification report; this historical cross-build inspection alone does not establish runtime success. Limited disk required discarding only regenerable build inputs and retrying NSIS packaging using hard-linked shared Electron/runtime inputs; the delivered EXEs contain complete independent data, not external links. `build/windows/repack-existing-manager.py` records this retry procedure. Native Windows CI remains the installation verification path.
