from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from .types import PreparedDataset, TemporalFold, TemporalPlan


@dataclass(frozen=True)
class TemporalSplitConfig:
    full_window_days: int = 120
    final_holdout_days: int = 30
    early_holdout_fraction: float = 0.20
    gap_hours: float = 2.0
    development_folds: int = 3
    initial_train_fraction: float = 0.40
    min_train_rows: int = 40
    min_test_rows: int = 10


class TemporalSplitter:
    """Deterministic expanding-window splits with a two-hour label gap."""

    def __init__(self, config: TemporalSplitConfig | None = None) -> None:
        self.config = config or TemporalSplitConfig()
        if not 0 < self.config.early_holdout_fraction < 1:
            raise ValueError("early_holdout_fraction must be between zero and one")
        if not 0 < self.config.initial_train_fraction < 1:
            raise ValueError("initial_train_fraction must be between zero and one")

    def build(self, dataset: PreparedDataset) -> TemporalPlan:
        if len(dataset) < self.config.min_train_rows + self.config.min_test_rows:
            raise ValueError("not enough mature rows for temporal training and validation")
        timestamps = dataset.timestamps.reset_index(drop=True)
        if not timestamps.is_monotonic_increasing:
            raise ValueError("prepared dataset must be ordered by event time")

        end = timestamps.iloc[-1]
        start = timestamps.iloc[0]
        span = end - start
        gap = pd.Timedelta(hours=self.config.gap_hours)
        early_stage = span < pd.Timedelta(days=self.config.full_window_days)

        if early_stage:
            active = np.arange(len(dataset), dtype=int)
            test_rows = max(
                self.config.min_test_rows,
                int(np.ceil(len(active) * self.config.early_holdout_fraction)),
            )
            if test_rows >= len(active):
                raise ValueError("early-stage holdout leaves no training rows")
            holdout_start_position = len(active) - test_rows
            final_test = active[holdout_start_position:]
            holdout_start = timestamps.iloc[final_test[0]]
            final_train = active[
                (timestamps.iloc[active] <= holdout_start - gap).to_numpy()
            ]
        else:
            active_start = end - pd.Timedelta(days=self.config.full_window_days)
            active = np.flatnonzero((timestamps >= active_start).to_numpy())
            holdout_start = end - pd.Timedelta(days=self.config.final_holdout_days)
            final_test = active[(timestamps.iloc[active] >= holdout_start).to_numpy()]
            final_train = active[(timestamps.iloc[active] <= holdout_start - gap).to_numpy()]

        if len(final_train) < self.config.min_train_rows:
            raise ValueError("two-hour gap leaves too few final training rows")
        if len(final_test) < self.config.min_test_rows:
            raise ValueError("final chronological holdout is too small")

        development_folds = self._expanding_folds(final_train, timestamps, gap)
        final_split = TemporalFold(
            name="final_recent_20pct" if early_stage else "final_recent_30d",
            train_indices=final_train,
            test_indices=final_test,
        )
        return TemporalPlan(
            development_folds=tuple(development_folds),
            final_split=final_split,
            refit_indices=active,
            active_indices=active,
            early_stage=early_stage,
            data_start=start.to_pydatetime(),
            data_end=end.to_pydatetime(),
            gap_hours=self.config.gap_hours,
        )

    def _expanding_folds(
        self,
        development_indices: np.ndarray,
        timestamps: pd.Series,
        gap: pd.Timedelta,
    ) -> list[TemporalFold]:
        count = len(development_indices)
        initial_rows = max(
            self.config.min_train_rows,
            int(np.floor(count * self.config.initial_train_fraction)),
        )
        remaining = development_indices[initial_rows:]
        if len(remaining) < self.config.min_test_rows:
            raise ValueError("development period is too small for expanding validation")

        max_folds = min(
            self.config.development_folds,
            max(1, len(remaining) // self.config.min_test_rows),
        )
        blocks = [block for block in np.array_split(remaining, max_folds) if len(block)]
        folds: list[TemporalFold] = []
        for number, test in enumerate(blocks, start=1):
            test_start = timestamps.iloc[test[0]]
            earlier = development_indices[development_indices < test[0]]
            train = earlier[
                (timestamps.iloc[earlier] <= test_start - gap).to_numpy()
            ]
            if len(train) < self.config.min_train_rows or len(test) < self.config.min_test_rows:
                continue
            folds.append(
                TemporalFold(
                    name=f"development_fold_{number}",
                    train_indices=train,
                    test_indices=test,
                )
            )
        if not folds:
            raise ValueError("could not create a valid expanding-window fold after the label gap")
        return folds
