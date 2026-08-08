import os
import sys
import subprocess
import platform
import venv

def run_command(cmd, shell=False, check=True):
    print(f"Running: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    try:
        subprocess.run(cmd, shell=shell, check=check)
    except subprocess.CalledProcessError as e:
        print(f"[Error] Command failed: {e}")
        sys.exit(1)

def main():
    print("==========================================")
    print("      db-agent Installation Wizard        ")
    print("==========================================")

    system = platform.system().lower()
    
    # 1. Check Python version
    if sys.version_info < (3, 12):
        print("[Error] Python 3.12+ is required.")
        sys.exit(1)
        
    # 2. Create virtual environment
    print("Creating virtual environment...")
    venv_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv")
    builder = venv.EnvBuilder(with_pip=True)
    builder.create(venv_dir)

    # 5. Install package
    print("Installing db-agent dependencies...")
    pip_exe = os.path.join(venv_dir, "Scripts", "pip.exe") if system == "windows" else os.path.join(venv_dir, "bin", "pip")
    run_command([pip_exe, "install", "-e", "."])

    # 6. Global link warning/info
    print("==========================================")
    print(" Setup complete!")
    print(f" Virtual environment created at: {venv_dir}")
    if system == "windows":
        print(" To run db-agent, activate the venv:")
        print(f"   {venv_dir}\\Scripts\\activate")
        print("   db-agent")
    else:
        local_bin = os.path.expanduser("~/.local/bin")
        os.makedirs(local_bin, exist_ok=True)
        target = os.path.join(local_bin, "db-agent")
        source = os.path.join(venv_dir, "bin", "db-agent")
        if os.path.exists(source):
            try:
                os.symlink(source, target)
                print(f" Linked to {target}.")
                print(" Ensure ~/.local/bin is in your PATH.")
            except FileExistsError:
                os.remove(target)
                os.symlink(source, target)
                print(f" Linked to {target}.")
        else:
            print(f" To run db-agent, use: {source}")

    print("==========================================")

if __name__ == "__main__":
    main()
