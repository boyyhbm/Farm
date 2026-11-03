"""Windows service host. Install with the fixed PowerShell installer."""
import asyncio
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import servicemanager
import win32event
import win32service
import win32serviceutil
from agent.config import Config
from agent.main import Agent, configure_logging
from security.credentials import read_credentials


class FarmAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "CS2FarmAgent"
    _svc_display_name_ = "CS2 Farm Node Agent"
    _svc_description_ = "Restricted CS2 infrastructure telemetry and managed process orchestration"

    def __init__(self, args):
        super().__init__(args)
        self.agent = None
        self.loop = None
        self.stop_requested = False

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_requested = True
        if self.agent and self.loop:
            self.loop.call_soon_threadsafe(self.agent.stopping.set)

    def SvcDoRun(self):
        os.environ["CS2_FARM_SERVICE"] = "1"
        config_path = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "CS2FarmAgent" / "config.json"
        try:
            asyncio.run(self.serve(config_path))
        except Exception as exc:
            servicemanager.LogErrorMsg(f"CS2 Farm Agent stopped: {type(exc).__name__}: {exc}")
            # Abnormal exit invokes SCM recovery policy; ordinary service stop stays stopped.
            os._exit(1)

    async def serve(self, config_path):
        config = Config.load(str(config_path))
        configure_logging(config)
        self.loop = asyncio.get_running_loop()
        self.agent = Agent(config, read_credentials(config.data_dir / "credentials.dat"))
        if self.stop_requested:
            self.agent.stopping.set()
        result = await self.agent.run()
        if result == 75:
            # Restart requested by authenticated server; SCM configured recovery restarts host.
            os._exit(1)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(FarmAgentService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(FarmAgentService)
