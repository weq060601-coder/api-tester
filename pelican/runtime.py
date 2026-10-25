"""Install the native-preview dependency in a project-local, isolated environment."""
from pathlib import Path
import subprocess
import sys
import threading

_LOCK = threading.Lock()


def ensure_runtime() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    # Reuse the installed venv at its original absolute path. Moving an
    # existing Windows venv would break its generated launchers.
    directory = Path(__file__).resolve().parent.parent / ".preview-venv"
    python = directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with _LOCK:
        if not python.is_file():
            subprocess.run([sys.executable, "-m", "venv", str(directory)], check=True, creationflags=flags)
        check = subprocess.run([str(python), "-c", "import webview"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        if check.returncode:
            subprocess.run([str(python), "-m", "pip", "install", "-r",
                            str(Path(__file__).with_name("requirements.txt"))],
                           check=True, creationflags=flags)
    return python


if __name__ == "__main__":
    print(f"Native preview ready: {ensure_runtime()}")
