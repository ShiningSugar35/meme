from __future__ import annotations

from pathlib import Path

from backend.app.database import Database
from backend.app.services.system_awake import SystemAwakeService


class FakePowerApi:
    def __init__(self, ac_line_status: int = 1) -> None:
        self.status = ac_line_status
        self.requests: list[bool] = []

    def ac_line_status(self) -> int:
        return self.status

    def set_system_required(self, required: bool) -> None:
        self.requests.append(required)


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "awake.db")
    database.initialize()
    return database


def test_awake_request_is_held_on_ac_and_released_on_battery(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    power = FakePowerApi(1)
    service = SystemAwakeService(database, power_api=power)

    service.sync_once()
    assert power.requests == [True]
    status = database.get_runtime_state("system_awake_request")
    assert status["state"] == "held_on_ac"
    assert status["system_required"] is True
    assert status["ac_line_status"] == 1

    power.status = 0
    service.sync_once()
    assert power.requests == [True, False]
    status = database.get_runtime_state("system_awake_request")
    assert status["state"] == "released_on_battery"
    assert status["system_required"] is False
    assert status["ac_line_status"] == 0


def test_awake_request_disabled_never_holds_system(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    power = FakePowerApi(1)
    service = SystemAwakeService(database, enabled=False, power_api=power)

    service.sync_once()

    assert power.requests == []
    status = database.get_runtime_state("system_awake_request")
    assert status["state"] == "disabled"
    assert status["system_required"] is False
