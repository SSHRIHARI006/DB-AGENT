import os
import platform
import subprocess
import sys
import venv


def run_command(cmd: list[str]) -> None:
    print(f"Running: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"[Error] Command failed with exit code {exc.returncode}.")
        sys.exit(exc.returncode)


def main() -> None:
    print("==========================================")
    print("      db-agent Installation Wizard        ")
    print("==========================================")

    if sys.version_info < (3, 12):
        print("[Error] Python 3.12+ is required.")
        sys.exit(1)

    project_dir = os.path.dirname(os.path.abspath(__file__))
    system = platform.system().lower()
    venv_dir = os.path.join(project_dir, ".venv")
    python_exe = os.path.join(
        venv_dir,
        "Scripts" if system == "windows" else "bin",
        "python.exe" if system == "windows" else "python",
    )

    if not os.path.exists(python_exe):
        print("Creating virtual environment...")
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    else:
        print(f"Using existing virtual environment: {venv_dir}")

    print("Installing db-agent dependencies...")
    run_command([python_exe, "-m", "pip", "install", "--upgrade", "pip"])
    run_command([python_exe, "-m", "pip", "install", "-e", project_dir])
    run_command([
        python_exe,
        "-c",
        "from mcp.server.fastmcp import FastMCP; print('MCP compatibility check passed')",
    ])

    print("==========================================")
    print(" Setup complete!")
    print(f" Virtual environment: {venv_dir}")
    if system == "windows":
        print(f" PowerShell activation: .\\.venv\\Scripts\\Activate.ps1")
        print(f" Command Prompt activation: .venv\\Scripts\\activate.bat")
        print(f" Run without activation: .\\.venv\\Scripts\\db-agent.exe")
        print(f" Or run: .\\.venv\\Scripts\\python.exe -m db_agent.cli")
    else:
        local_bin = os.path.expanduser("~/.local/bin")
        os.makedirs(local_bin, exist_ok=True)
        target = os.path.join(local_bin, "db-agent")
        source = os.path.join(project_dir, "db-agent")
        try:
            if os.path.lexists(target):
                os.remove(target)
            os.symlink(source, target)
            print(f" Linked to {target}.")
        except OSError:
            print(f" Run directly with: {source}")
    print(" Configure a provider with /provider set <name>.")
    print("==========================================")


if __name__ == "__main__":
    main()
