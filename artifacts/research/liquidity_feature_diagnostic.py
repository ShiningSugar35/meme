from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from backend.app.collector.constants import FEATURE_SCHEMA_VERSION, LabelPolicy
from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.ml.features import FeatureBuilder, FeaturePolicy
from backend.app.services.training import TrainingService


def main() -> None:
    settings = get_settings()
    db = Database(settings.database_path)
    service = TrainingService(db, settings)
    rows = [
        row for row in service.samples.list_mature()
        if row.get("feature_schema_version") == FEATURE_SCHEMA_VERSION
        and row.get("label_version") == LabelPolicy().label_version
        and row.get("token_type") in {"new_creation", "near_completion"}
    ]
    frame, _ = service._training_frame(rows)
    dataset = FeatureBuilder(FeaturePolicy(feature_allowlist=service.configured_feature_selection())).prepare(frame)
    names = [
        name for name in (
            "ln(liquidity_usd)",
            "liquidity/holder_count",
            "ln(marketcap+1)",
            "stat.holder_count/market_cap",
            "volume_1h/swaps_1h",
        ) if name in dataset.X.columns
    ]
    corr = dataset.X[names].corr(method="spearman")
    latest = db.fetch_one("SELECT summary_json FROM training_runs WHERE status='completed' ORDER BY completed_at DESC LIMIT 1")
    summary = json.loads(latest.get("summary_json") or "{}") if latest else {}
    cert = summary.get("deployment_certification") or {}
    psi = {}
    for model in cert.get("models") or []:
        drift = model.get("final_drift") or {}
        psi[model.get("algorithm")] = {
            name: drift.get("feature_psi", {}).get(name)
            for name in names
            if name in (drift.get("feature_psi") or {})
        }
    out = {
        "rows": len(dataset.X),
        "spearman": {row: {col: float(corr.loc[row, col]) for col in names} for row in names},
        "latest_final_psi": psi,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
