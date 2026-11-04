import os

import pytest
from agent import service_bridge as bridge


def test_service_identity_is_not_an_arbitrary_scm_interface():
    for name in ("Spooler", "../CS2FarmAgent", "", "CS2FarmAgentExtra"):
        with pytest.raises(ValueError):
            bridge.paths(name)
    assert bridge.paths("CS2FarmAgent")[1].name == "CS2FarmAgent"


def test_service_uses_fixed_runtime_and_safely_quoted_local_paths(tmp_path):
    runtime = tmp_path / "runtime with spaces"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"test")
    (runtime / "desktop_entry.py").write_text("test")
    value = bridge._command(runtime, name="CS2FarmAgentTestA")
    assert "service-host --service-name CS2FarmAgentTestA" in value
    assert '"' in value
    with pytest.raises(ValueError):
        bridge._command(runtime, name="Spooler")


def test_service_setup_rejects_remote_commands_before_touching_scm(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(bridge, "_windows", lambda **kw: None)
    with pytest.raises(ValueError, match="field"):
        bridge.install(
            {"server_url": "https://example.invalid", "command": "cmd.exe"}, tmp_path
        )
    for url in (
        "http://127.0.0.1",
        "https://user:pass@example.com",
        "https://example.com/?token=secret",
    ):
        with pytest.raises(ValueError, match="HTTPS"):
            bridge.install({"server_url": url, "token": "t" * 30}, tmp_path)


@pytest.mark.skipif(
    os.name == "nt", reason="Platform rejection is a non-Windows contract"
)
def test_non_windows_cannot_report_a_working_windows_service():
    with pytest.raises(RuntimeError, match="only on Windows"):
        bridge.status()
