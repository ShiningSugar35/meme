from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
HOST = "127.0.0.1"
PORT = 8000
POLL_SECONDS = 0.8
DEBOUNCE_SECONDS = 0.35


def source_snapshot() -> tuple[tuple[str, int, int], ...]:
    items: list[tuple[str, int, int]] = []
    for path in BACKEND_ROOT.rglob("*.py"):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append((str(path.relative_to(PROJECT_ROOT)), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(items))


def child_command() -> list[str]:
    return [
        str(PYTHON),
        "-m",
        "uvicorn",
        "backend.app.main:app",
        "--host",
        HOST,
        "--port",
        str(PORT),
    ]


def start_child() -> subprocess.Popen[bytes]:
    flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        if os.name == "nt"
        else 0
    )
    process = subprocess.Popen(
        child_command(),
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    print(f"runtime_supervisor=child_started pid={process.pid}", flush=True)
    return process


def stop_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    else:  # pragma: no cover - Windows is the project deployment target.
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    if not PYTHON.exists():
        print(f"runtime_supervisor=blocked reason=python_missing path={PYTHON}", flush=True)
        return 2
    snapshot = source_snapshot()
    child = start_child()
    try:
        while True:
            time.sleep(POLL_SECONDS)
            next_snapshot = source_snapshot()
            if next_snapshot != snapshot:
                time.sleep(DEBOUNCE_SECONDS)
                next_snapshot = source_snapshot()
                print("runtime_supervisor=backend_change_detected action=restart", flush=True)
                stop_child(child)
                child = start_child()
                snapshot = next_snapshot
                continue
            if child.poll() is not None:
                exit_code = child.returncode
                print(f"runtime_supervisor=child_exited code={exit_code} action=restart", flush=True)
                time.sleep(1.0)
                child = start_child()
    except KeyboardInterrupt:
        return 0
    finally:
        stop_child(child)


if __name__ == "__main__":
    raise SystemExit(main())
