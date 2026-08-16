from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import Database
from backend.app.ml.family_audit import FeatureFamilyAuditService


def main() -> int:
    database = Database(PROJECT_ROOT / "data" / "meme_quant.db")
    database.initialize()
    report = FeatureFamilyAuditService(database).run()
    target = PROJECT_ROOT / "artifacts" / "feature_family_audit.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "generation": report.get("generation"),
                "mature_samples": (report.get("readiness") or {}).get("mature_samples"),
                "artifact": str(target.relative_to(PROJECT_ROOT)),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
