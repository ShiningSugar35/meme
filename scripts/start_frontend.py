from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
LOG_DIR = PROJECT_ROOT / "logs"
PID_FILE = LOG_DIR / "frontend.pid"
OUT_FILE = LOG_DIR / "frontend.out.log"
ERR_FILE = LOG_DIR / "frontend.err.log"
HOST = "127.0.0.1"
PORT = 5173


def port_is_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, PORT)) == 0


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if port_is_open():
        print(f"frontend=already_running host={HOST} port={PORT}")
        return 0

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        print("frontend=blocked reason=npm_missing")
        return 2

    command: list[str] | str = [npm, "run", "dev"]
    creationflags = 0
    use_shell = False
    if os.name == "nt":
        command = subprocess.list2cmdline([npm, "run", "dev"])
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        use_shell = True

    out_handle = OUT_FILE.open("ab", buffering=0)
    err_handle = ERR_FILE.open("ab", buffering=0)
    process = subprocess.Popen(
        command,
        cwd=FRONTEND_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=out_handle,
        stderr=err_handle,
        creationflags=creationflags,
        close_fds=True,
        shell=use_shell,
    )
    PID_FILE.write_text(str(process.pid), encoding="ascii")
    print(f"frontend=started pid={process.pid} host={HOST} port={PORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
