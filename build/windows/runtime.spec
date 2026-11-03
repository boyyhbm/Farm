# Windows-only PyInstaller analysis. It bundles Python, native extension DLLs and all application code.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

root = Path(SPECPATH).resolve().parents[1]
api = root / 'apps' / 'api'
agent = root / 'apps' / 'windows-agent'
engine = root / 'packages' / 'gameplay-test-engine'
# Collection helpers run before Analysis, so they need the same source import paths.
sys.path[:0] = [str(api), str(agent), str(engine)]
datas = [
    (str(api / 'migrations'), 'server/migrations'),
    (str(api / 'alembic.ini'), 'server'),
    (str(agent / 'installer'), 'agent-installer'),
    (str(root / 'build/windows/node-runtime.json'), 'build-metadata'),
]
hiddenimports = []
binaries = []
for package in ('app', 'agent', 'collectors', 'process_manager', 'transport', 'security',
                'gameplay_test_engine', 'uvicorn', 'sqlalchemy', 'alembic', 'websockets',
                'argon2', 'celery', 'redis', 'psycopg', 'pydantic_settings'):
    hiddenimports += collect_submodules(package)
    datas += collect_data_files(package)
    binaries += collect_dynamic_libs(package)
hiddenimports += ['sqlite3', '_sqlite3', 'win32api', 'win32con', 'win32event', 'win32service',
                  'win32serviceutil', 'servicemanager', 'win32security', 'win32process', 'win32profile',
                  'win32ts', 'win32job', 'win32crypt', 'win32timezone', 'psutil._psutil_windows']
a = Analysis([str(api / 'desktop_entry.py')],
             pathex=[str(api), str(agent), str(engine)],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['pytest', 'tkinter', 'unittest', 'IPython', 'matplotlib'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='farm-runtime',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          # Electron launches this child with windowsHide=true; console mode preserves stdin/stdout
          # for authenticated setup and structured health diagnostics without a visible console.
          console=True, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='farm-runtime')
