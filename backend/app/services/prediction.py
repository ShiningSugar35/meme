from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..config import PROJECT_ROOT, Settings, get_settings
from ..database import Database, utc_now_iso
from ..ml.registry import ModelRegistry
from ..repositories.models import ModelRepository
from .paper_trading import PaperTradingService


_PROFILE_ACCOUNTS = {
    "aggressive": "shadow_aggressive",
    "balanced": "paper",
    "conservative": "shadow_conservative",
}


@dataclass(frozen=True, slots=True)
class PredictionCycleResult:
    model_id: str | None
    samples_scored: int = 0
    predictions_written: int = 0
    signals_selected: int = 0
    paper_positions_opened: int = 0
    paper_positions_settled: int = 0
    stale_signals: int = 0
    blocked_signals: int = 0
    reason: str = "ok"


class PredictionService:
    """Score post-training samples and persist the three threshold profiles.

    Scoring is allowed for historical rows so OOS monitoring can be rebuilt,
    but a simulated/live action may only be created while the signal is fresh.
    This prevents a restart from retroactively inventing fills hours later.
    """

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.models = ModelRepository(database)
        self.paper = PaperTradingService(database, self.settings)

    def run_cycle(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> PredictionCycleResult:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        champion = self.models.champion()
        if not champion:
            settled = (
                0
                if self.settings.paper_market_monitor_enabled
                else self.paper.settle_mature_positions()
            )
            return PredictionCycleResult(
                model_id=None,
                paper_positions_settled=settled,
                reason="no_champion_model",
            )

        bundle = self._load_bundle(champion)
        rows = self.database.fetch_all(
            """
            SELECT s.*
            FROM samples s
            WHERE s.entry_time > COALESCE(?, 0)
              AND NOT EXISTS(
                  SELECT 1 FROM predictions p
                  WHERE p.sample_id=s.id AND p.model_id=?
              )
            ORDER BY s.entry_time, s.id
            LIMIT ?
            """,
            (champion.get("training_window_end"), champion["id"], limit),
        )

        predictions_written = 0
        selected = 0
        for row in rows:
            frame = self._prediction_frame(row, bundle.feature_names)
            probability = float(bundle.predict_probabilities(frame)[0])
            probability = float(np.clip(probability, 0.0, 1.0))
            with self.database.transaction(immediate=True) as connection:
                for profile in ("aggressive", "balanced", "conservative"):
                    threshold = bundle.thresholds.for_profile(profile)
                    chosen = probability >= threshold
                    cursor = connection.execute(
                        """
                        INSERT INTO predictions(
                            sample_id, model_id, probability, profile, threshold,
                            selected, predicted_at
                        ) VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(sample_id, model_id, profile) DO NOTHING
                        """,
                        (
                            row["id"], champion["id"], probability, profile,
                            threshold, int(chosen), moment.isoformat(),
                        ),
                    )
                    if cursor.rowcount == 1:
                        predictions_written += 1
                        selected += int(chosen)

        opened, stale, blocked = self._reconcile_paper_signals(
            champion["id"], moment=moment
        )
        settled = (
            0
            if self.settings.paper_market_monitor_enabled
            else self.paper.settle_mature_positions()
        )
        result = PredictionCycleResult(
            model_id=champion["id"],
            samples_scored=len(rows),
            predictions_written=predictions_written,
            signals_selected=selected,
            paper_positions_opened=opened,
            paper_positions_settled=settled,
            stale_signals=stale,
            blocked_signals=blocked,
        )
        self.database.set_runtime_state("prediction_worker_last_cycle", asdict(result))
        return result

    def _load_bundle(self, champion: dict[str, Any]) -> Any:
        artifact = Path(champion["artifact_path"])
        artifact = artifact if artifact.is_absolute() else PROJECT_ROOT / artifact
        if not artifact.exists():
            raise FileNotFoundError(f"champion artifact is missing: {artifact.name}")
        return ModelRegistry(artifact.parent).load(artifact.stem)

    @staticmethod
    def _prediction_frame(row: dict[str, Any], feature_names: tuple[str, ...]) -> pd.DataFrame:
        try:
            source = json.loads(row.get("features_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            source = {}
        # Production inputs come from the frozen entry-time allowlist. Launchpad
        # remains metadata and is deliberately excluded from the model schema.
        source["price"] = row.get("entry_price")
        record = {name: source.get(name, np.nan) for name in feature_names}
        return pd.DataFrame.from_records([record], columns=list(feature_names))

    def _reconcile_paper_signals(
        self,
        model_id: str,
        *,
        moment: datetime,
    ) -> tuple[int, int, int]:
        if not self.settings.simulation_enabled:
            return 0, 0, 0
        rows = self.database.fetch_all(
            """
            SELECT p.id AS prediction_id, p.sample_id, p.profile, p.model_id,
                   s.entry_time
            FROM predictions p
            JOIN samples s ON s.id=p.sample_id
            WHERE p.model_id=? AND p.selected=1
              AND p.profile IN ('aggressive','balanced','conservative')
              AND NOT EXISTS(
                  SELECT 1 FROM positions pos
                  WHERE pos.prediction_id=p.id
                    AND pos.account_kind=CASE p.profile
                        WHEN 'aggressive' THEN 'shadow_aggressive'
                        WHEN 'balanced' THEN 'paper'
                        ELSE 'shadow_conservative'
                    END
              )
            ORDER BY s.entry_time, p.id
            """,
            (model_id,),
        )
        opened = stale = blocked = 0
        now_epoch = int(moment.timestamp())
        for row in rows:
            if now_epoch - int(row["entry_time"]) > self.settings.signal_max_age_seconds:
                stale += 1
                continue
            account = _PROFILE_ACCOUNTS[row["profile"]]
            result = self.paper.open_from_prediction(
                sample_id=int(row["sample_id"]),
                prediction_id=int(row["prediction_id"]),
                model_id=row["model_id"],
                profile=row["profile"],
                account=account,
            )
            if result.opened:
                opened += 1
            elif result.reason != "already_opened":
                blocked += 1
        return opened, stale, blocked


class PredictionWorker:
    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.service = PredictionService(database, self.settings)
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        self.database.set_runtime_state(
            "prediction_worker_status", {"state": "running", "started_at": utc_now_iso()}
        )
        while not self._stop.is_set():
            try:
                result = await asyncio.to_thread(self.service.run_cycle)
                self.database.set_runtime_state(
                    "prediction_worker_status",
                    {"state": "running", "last_cycle_at": utc_now_iso(), **asdict(result)},
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                self.database.set_runtime_state(
                    "prediction_worker_status",
                    {"state": "degraded", "last_cycle_at": utc_now_iso(), "error": message},
                )
                self.database.audit(
                    category="prediction",
                    action="cycle_failed",
                    severity="error",
                    details={"error": message},
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=float(self.settings.signal_poll_seconds)
                )
            except TimeoutError:
                pass
        self.database.set_runtime_state(
            "prediction_worker_status", {"state": "stopped", "stopped_at": utc_now_iso()}
        )

    def stop(self) -> None:
        self._stop.set()
