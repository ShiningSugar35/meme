from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np

from .economics import evaluate_probabilities
from .types import ModelBundle, PreparedDataset, PromotionDecision


@dataclass(frozen=True)
class PromotionConfig:
    min_pnl_lift: float = 0.05
    minimum_baseline_usd: float = 1.0
    min_trades: int = 3
    label_gap_hours: float = 2.0


class PromotionEvaluator:
    """Compatibility evaluator for two fixed recipes on the same OOS rows.

    Top-3 training no longer uses incumbent-vs-challenger promotion. This class
    remains for rollback/audit tests and compares each bundle at its own single
    development-frozen threshold.
    """

    def __init__(self, config: PromotionConfig | None = None) -> None:
        self.config = config or PromotionConfig()

    def compare(
        self,
        candidate: ModelBundle,
        incumbent: ModelBundle,
        dataset: PreparedDataset,
        comparison_indices: np.ndarray,
    ) -> PromotionDecision:
        rows = np.asarray(comparison_indices, dtype=int)
        if len(rows) == 0:
            raise ValueError("promotion comparison window is empty")
        X = dataset.X.iloc[rows]
        candidate_probs = candidate.predict_probabilities(X)
        incumbent_probs = incumbent.predict_probabilities(X)
        economics = dataset.economic_slice(rows)
        y = dataset.y.iloc[rows].to_numpy(dtype=int)

        candidate_metrics = evaluate_probabilities(
            y, candidate_probs, candidate.threshold, economics
        )
        incumbent_metrics = evaluate_probabilities(
            y, incumbent_probs, incumbent.threshold, economics
        )

        blockers = list(economics.blockers)
        comparison_start = dataset.timestamps.iloc[rows].min().to_pydatetime()
        for role, bundle in (("candidate", candidate), ("incumbent", incumbent)):
            if bundle.metrics.get("evaluation_only") is not True:
                blockers.append(
                    f"{role} comparison model is not marked as a pre-holdout evaluation bundle"
                )
            if bundle.training_end + timedelta(hours=self.config.label_gap_hours) > comparison_start:
                blockers.append(f"{role} model training overlaps the shared comparison window")
        if not economics.utility_eligible:
            blockers.append(
                "automatic promotion requires real entry-time liquidity for every row in the shared comparison window"
            )
        if candidate_metrics.trade_count < self.config.min_trades:
            blockers.append(
                f"candidate produced only {candidate_metrics.trade_count} trades; at least {self.config.min_trades} are required"
            )

        candidate_pnl = candidate_metrics.cumulative_pnl_usd
        incumbent_pnl = incumbent_metrics.cumulative_pnl_usd
        lift: float | None = None
        if candidate_pnl is not None and incumbent_pnl is not None:
            denominator = max(abs(incumbent_pnl), self.config.minimum_baseline_usd)
            lift = (candidate_pnl - incumbent_pnl) / denominator
            required = incumbent_pnl + self.config.min_pnl_lift * denominator
            if candidate_pnl < required:
                blockers.append(
                    f"candidate OOS PnL {candidate_pnl:.6f} did not exceed incumbent {incumbent_pnl:.6f} by the required {self.config.min_pnl_lift:.2%} normalized margin"
                )

        eligible = not blockers
        return PromotionDecision(
            eligible=eligible,
            promote=eligible,
            candidate_metrics=candidate_metrics,
            incumbent_metrics=incumbent_metrics,
            pnl_lift=lift,
            blockers=tuple(dict.fromkeys(blockers)),
            comparison_rows=len(rows),
        )
