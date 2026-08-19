from __future__ import annotations

import asyncio
import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Protocol

from ..database import Database, utc_now_iso


ES_SYSTEM_REQUIRED = 0x00000001
ES_CONTINUOUS = 0x80000000


class PowerApi(Protocol):
    def ac_line_status(self) -> int: ...

    def set_system_required(self, required: bool) -> None: ...


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class WindowsPowerApi:
    """Keep Windows awake for critical background collection without forcing the display on."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WindowsPowerApi is only available on Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SystemPowerStatus)]
        self.kernel32.GetSystemPowerStatus.restype = wintypes.BOOL
        self.kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        self.kernel32.SetThreadExecutionState.restype = wintypes.DWORD

    def ac_line_status(self) -> int:
        status = _SystemPowerStatus()
        if not self.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            raise OSError(ctypes.get_last_error(), "GetSystemPowerStatus failed")
        return int(status.ACLineStatus)

    def set_system_required(self, required: bool) -> None:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if required else 0)
        if self.kernel32.SetThreadExecutionState(flags) == 0:
            raise OSError(ctypes.get_last_error(), "SetThreadExecutionState failed")


@dataclass(slots=True)
class SystemAwakeService:
    database: Database
    poll_seconds: float = 30.0
    enabled: bool = True
    power_api: PowerApi | None = None
    _stop: asyncio.Event = field(init=False, repr=False)
    _held: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        self._stop = asyncio.Event()
        if self.power_api is None and sys.platform == "win32":
            self.power_api = WindowsPowerApi()

    def stop(self) -> None:
        self._stop.set()

    def _status(self, *, state: str, ac_line_status: int | None, error: str | None = None) -> None:
        payload = {
            "state": state,
            "enabled": bool(self.enabled),
            "platform": sys.platform,
            "ac_line_status": ac_line_status,
            "system_required": bool(self._held),
            "updated_at": utc_now_iso(),
        }
        if error:
            payload["last_error"] = error[:500]
        self.database.set_runtime_state("system_awake_request", payload)

    def sync_once(self) -> None:
        if not self.enabled:
            if self.power_api is not None and self._held:
                self.power_api.set_system_required(False)
                self._held = False
            self._status(state="disabled", ac_line_status=None)
            return

        if self.power_api is None:
            self._status(state="unsupported_platform", ac_line_status=None)
            return

        ac_line_status = self.power_api.ac_line_status()
        should_hold = ac_line_status == 1
        if should_hold != self._held:
            self.power_api.set_system_required(should_hold)
            self._held = should_hold
        self._status(
            state="held_on_ac" if self._held else "released_on_battery",
            ac_line_status=ac_line_status,
        )

    async def run_forever(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.sync_once()
                except Exception as exc:
                    self._status(
                        state="degraded",
                        ac_line_status=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(5.0, self.poll_seconds))
                except TimeoutError:
                    pass
        finally:
            if self.power_api is not None and self._held:
                try:
                    self.power_api.set_system_required(False)
                finally:
                    self._held = False
            self._status(state="released_shutdown", ac_line_status=None)
