from __future__ import annotations

from datetime import datetime, timezone

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.services.prediction import PredictionService


def test_prediction_gate_uses_rules_only_below_mature_sample_floor(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "gate.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "gate.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=1_000,
        paper_market_monitor_enabled=False,
    )
    service = PredictionService(database, settings)
    calls = []

    def fake_rules_only(**kwargs):
        calls.append(kwargs)
        return 2, 1, 0

    monkeypatch.setattr(service, "_reconcile_rule_only", fake_rules_only)
    monkeypatch.setattr(service.paper, "settle_mature_positions", lambda: 3)

    result = service.run_cycle(now=datetime.now(timezone.utc))

    assert calls and calls[0]["ignore_model_rollover_gate"] is True
    assert result.model_ids == ()
    assert result.predictions_written == 0
    assert result.model_positions_opened == 0
    assert result.rule_positions_opened == 2
    assert result.stale_signals == 1
