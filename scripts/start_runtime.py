from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"
PID_FILE = LOG_DIR / "backend.pid"
OUT_FILE = LOG_DIR / "backend.out.log"
ERR_FILE = LOG_DIR / "backend.err.log"
HOST = "127.0.0.1"
PORT = 8000


def port_is_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, PORT)) == 0


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if port_is_open():
        print(f"runtime=already_running host={HOST} port={PORT}")
        return 0

    python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        print(f"runtime=blocked reason=python_missing path={python}")
        return 2

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    out_handle = OUT_FILE.open("ab", buffering=0)
    err_handle = ERR_FILE.open("ab", buffering=0)
    process = subprocess.Popen(
        [
            str(python),
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--host",
            HOST,
            "--port",
            str(PORT),
        ],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=out_handle,
        stderr=err_handle,
        creationflags=creationflags,
        close_fds=True,
    )
    PID_FILE.write_text(str(process.pid), encoding="ascii")
    print(f"runtime=started pid={process.pid} host={HOST} port={PORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
