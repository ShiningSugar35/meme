from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.services.live_trading import build_execution_policy


def test_execution_policy_is_monotone_capped_and_has_extra_sell_attempt(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, app_env="test", sqlite_path=str(tmp_path / "db.sqlite"))
    policy = build_execution_policy(settings)
    assert len(policy.buy_steps) == 6
    assert len(policy.sell_steps) == 7
    assert policy.buy_steps[-1].priority_fee_sol + policy.buy_steps[-1].tip_fee_sol <= 0.006
    assert [step.slippage for step in policy.buy_steps] == sorted(step.slippage for step in policy.buy_steps)


def test_settings_reject_fee_above_hard_cap(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            app_env="test",
            sqlite_path=str(tmp_path / "db.sqlite"),
            trade_priority_fee_high_sol=0.006,
            trade_tip_fee_high_sol=0.001,
            trade_total_fee_cap_sol=0.006,
        )

