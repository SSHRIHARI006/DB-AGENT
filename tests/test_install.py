import importlib.util
from pathlib import Path


_INSTALL_PATH = Path(__file__).parents[1] / "install.py"
_SPEC = importlib.util.spec_from_file_location("db_agent_install", _INSTALL_PATH)
install = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(install)


def test_windows_installation_prints_shell_specific_commands(monkeypatch, capsys):
    commands = []
    monkeypatch.setattr(install.platform, "system", lambda: "Windows")
    monkeypatch.setattr(install.os.path, "exists", lambda _: True)
    monkeypatch.setattr(install, "run_command", commands.append)

    install.main()

    output = capsys.readouterr().out
    assert "PowerShell activation: .\\.venv\\Scripts\\Activate.ps1" in output
    assert "Command Prompt activation: .venv\\Scripts\\activate.bat" in output
    assert "Run without activation: .\\.venv\\Scripts\\db-agent.exe" in output
    assert "Or run: .\\.venv\\Scripts\\python.exe -m db_agent.cli" in output
    assert commands[0][1:4] == ["-m", "pip", "install"]
