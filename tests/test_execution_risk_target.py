import json

import numpy as np
import pytest

from backend.app.ml.calibration import SigmoidCalibrator
from backend.app.services.execution_risk import ExecutionRiskModel, ExecutionRiskTrainer


class _FixedEstimator:
    def predict_proba(self, _x):
        return np.asarray([[0.5, 0.5]], dtype=float)


def _row(exit_reason: str, trigger: float = 0.0, stop: float = 1.0) -> dict:
    return {
        "exit_reason": exit_reason,
        "stop_loss_price": stop,
        "metadata_json": json.dumps({"exit_trigger_reference_price": trigger}),
    }


def test_execution_risk_target_is_unconditional_over_all_closed_entries() -> None:
    assert ExecutionRiskTrainer._target(_row("take_profit_1_8x", trigger=0.1)) == 0
    assert ExecutionRiskTrainer._target(_row("timeout_90m", trigger=0.1)) == 0
    assert ExecutionRiskTrainer._target(_row("stop_loss_0_9x", trigger=0.95)) == 0
    assert ExecutionRiskTrainer._target(_row("stop_loss_0_9x", trigger=0.89)) == 1


def test_execution_risk_stop_target_fails_closed_when_trigger_fact_missing() -> None:
    row = {"exit_reason": "stop_loss_0_9x", "stop_loss_price": 1.0, "metadata_json": "{}"}
    assert ExecutionRiskTrainer._target(row) is None


def test_execution_risk_probability_uses_chronological_calibrator() -> None:
    calibrator = SigmoidCalibrator(
        method="chronological_sigmoid_platt_v1",
        coefficient=2.0,
        intercept=-2.0,
        source_rows=100,
        source_index_hash="x" * 64,
    )
    model = ExecutionRiskModel(
        version="test",
        algorithm="fixed",
        feature_names=(),
        medians={},
        estimator=_FixedEstimator(),
        training_hash="hash",
        calibrator=calibrator,
    )
    assert model.predict_probability({}) == pytest.approx(1.0 / (1.0 + np.exp(1.0)))
    assert model.provenance()["calibration"]["source_rows"] == 100


class _RecordingDatabase:
    def __init__(self) -> None:
        self.query = ""
        self.parameters = ()

    def fetch_all(self, query, parameters):
        self.query = query
        self.parameters = parameters
        return []


def test_execution_risk_training_cutoff_excludes_final_and_label_gap_rows() -> None:
    database = _RecordingDatabase()
    trainer = ExecutionRiskTrainer(database)  # type: ignore[arg-type]

    result = trainer.train(max_entry_time_exclusive=1_234_567)

    assert "s.entry_time < ?" in database.query
    assert database.parameters[-1] == 1_234_567
    assert result.training_cutoff_entry_time_exclusive == 1_234_567
