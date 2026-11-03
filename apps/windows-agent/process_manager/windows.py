"""Fixed-argv process creation in the active console session from LocalSystem service."""
from __future__ import annotations
import subprocess
import time


class InteractiveProcess:
    def __init__(self, process_handle, thread_handle, pid):
        self.handle = process_handle
        self.thread = thread_handle
        self.pid = pid

    def resume(self):
        import win32process
        win32process.ResumeThread(self.thread)
        self.thread.Close()
        self.thread = None

    def close(self):
        if self.thread:
            self.thread.Close()
            self.thread = None
        if self.handle:
            self.handle.Close()
            self.handle = None

    def poll(self):
        import win32process
        value = win32process.GetExitCodeProcess(self.handle)
        return None if value == 259 else value

    def wait(self, timeout=None):
        import win32event
        result = win32event.WaitForSingleObject(self.handle, win32event.INFINITE if timeout is None else int(timeout * 1000))
        if result == win32event.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired("managed interactive process", timeout)
        return self.poll()

    def terminate(self):
        import win32api
        win32api.TerminateProcess(self.handle, 1)


def launch_interactive(argv):
    import win32api
    import win32con
    import win32process
    import win32security
    import win32ts
    import win32profile
    session_id = win32ts.WTSGetActiveConsoleSessionId()
    if session_id == 0xFFFFFFFF:
        raise RuntimeError("No active Windows console user; log in manually first")
    token = win32ts.WTSQueryUserToken(session_id)
    primary = None
    try:
        primary = win32security.DuplicateTokenEx(token, win32security.SecurityImpersonation,
                                                win32con.TOKEN_ALL_ACCESS, win32security.TokenPrimary)
        startup = win32process.STARTUPINFO()
        startup.lpDesktop = "winsta0\\default"
        environment = win32profile.CreateEnvironmentBlock(primary, False)
        handle, thread, pid, _ = win32process.CreateProcessAsUser(
            primary, argv[0], subprocess.list2cmdline(argv), None, None, False,
            win32con.CREATE_UNICODE_ENVIRONMENT | win32con.CREATE_NEW_PROCESS_GROUP | win32con.CREATE_SUSPENDED,
            environment, None, startup)
        return InteractiveProcess(handle, thread, pid)
    finally:
        token.Close()
        if primary:
            primary.Close()


def attach_job(pid):
    """Windows job owns descendants and kills them if the agent crashes."""
    import win32api
    import win32con
    import win32job
    job = win32job.CreateJobObject(None, None)
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    process = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
    try:
        win32job.AssignProcessToJobObject(job, process)
    except Exception:
        job.Close()
        raise
    finally:
        process.Close()
    return job
