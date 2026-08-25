from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from typing import Iterable

import numpy as np
from sklearn.linear_model import LogisticRegression


@dataclass(frozen=True, slots=True)
class SigmoidCalibrator:
    method: str
    coefficient: float
    intercept: float
    source_rows: int
    source_index_hash: str
    source_start: str | None = None
    source_end: str | None = None

    def transform(self, scores: np.ndarray | list[float]) -> np.ndarray:
        values = np.asarray(scores, dtype=float)
        if not np.isfinite(values).all():
            raise ValueError("calibration scores must be finite")
        logits = np.clip(self.coefficient * values + self.intercept, -50.0, 50.0)
        return 1.0 / (1.0 + np.exp(-logits))

    def provenance(self) -> dict[str, object]:
        return asdict(self)


def _source_hash(indices: Iterable[int]) -> str:
    digest = hashlib.sha256()
    for value in indices:
        digest.update(f"{int(value)}\n".encode())
    return digest.hexdigest()


def fit_sigmoid_calibrator(
    scores: np.ndarray | list[float],
    labels: np.ndarray | list[int],
    *,
    source_indices: Iterable[int],
    source_start: str | None = None,
    source_end: str | None = None,
) -> SigmoidCalibrator:
    x = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=int)
    indices = tuple(int(value) for value in source_indices)
    if len(x) != len(y) or len(x) != len(indices):
        raise ValueError("calibrator scores, labels and source indices must align")
    if len(x) < 20:
        raise ValueError("at least 20 development OOS rows are required for calibration")
    if not np.isfinite(x).all():
        raise ValueError("calibrator scores must be finite")
    if set(np.unique(y)) != {0, 1}:
        raise ValueError("sigmoid calibration requires both classes")
    model = LogisticRegression(C=1_000_000.0, solver="lbfgs", max_iter=2_000)
    model.fit(x.reshape(-1, 1), y)
    coefficient = float(model.coef_[0, 0])
    intercept = float(model.intercept_[0])
    if not np.isfinite([coefficient, intercept]).all() or coefficient <= 0:
        raise ValueError("sigmoid calibrator is not finite and monotonic increasing")
    return SigmoidCalibrator(
        method="chronological_sigmoid_platt_v1",
        coefficient=coefficient,
        intercept=intercept,
        source_rows=len(x),
        source_index_hash=_source_hash(indices),
        source_start=source_start,
        source_end=source_end,
    )
