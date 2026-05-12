from datetime import datetime, timedelta, timezone
from typing import Dict, Any


class MockData:
    """Deterministic mock data shaped like normalized GMGN payloads plus common GMGN aliases.

    The mock intentionally carries both internal schema names and API-like aliases so
    field-mapping bugs show up during tests rather than being hidden by one naming style.
    """

    def __init__(self):
        now = datetime.now(timezone.utc)
        self._last_refresh = now
        self._refresh_interval = timedelta(seconds=30)

        self._base_token_def: Dict[str, Any] = {
            "type": "new_creation",
            "launchpad": "Pump.fun",
            "platform": "Pump.fun",
            "liquidity_usd": 20_000.0,
            "liquidity": 20_000.0,
            "volume_usd": 15_000.0,
            "volume": 15_000.0,
            "market_cap": 85_000.0,
            "marketcap": 85_000.0,
            "price_usd": 0.000085,
            "price": 0.000085,
            "price_sol": 0.00000042,
            "sol_side_liquidity": 100.0,
            "sol_liquidity": 100.0,
            "top_10_holder_rate": 0.18,
            "top10_holder_rate": 0.18,
            "top1_holder_rate": 0.04,
            "renounced_mint": 1,
            "mint_renounced": True,
            "renounced_freeze_account": 1,
            "freeze_authority_renounced": True,
            "max_rug_ratio": -0.1,
            "max_insider_ratio": -0.1,
            "max_entrapment_ratio": -0.1,
            "is_wash_trading": 0,
            "rat_trader_amount_rate": -0.1,
            "suspected_insider_hold_rate": 0.01,
            "max_bundler_rate": -0.1,
            "fresh_wallet_rate": 0.05,
            "sell_tax": 0.0,
            "has_social": 1,
            "twitter": "https://x.com/mock",
            "telegram": "https://t.me/mock",
            "creator_token_status": "creator_close",
            "dev_team_hold_rate": 0.0,
            "dev_token_burn_ratio": 1.0,
            "sniper_count": 1,
            "symbol": "MOCK",
            "name": "Mock Token",
        }

        self.tokens = {
            "PASS1": self._token("PASS1"),
            "PASS1_150": self._token("PASS1_150"),
            "PASS1_510": self._token("PASS1_510"),
            "FAIL_INIT": {
                "token_mint": "FAIL_INIT",
                "address": "FAIL_INIT",
                "token_address": "FAIL_INIT",
                "type": "new_creation",
                "launchpad": "Pump.fun",
                "platform": "Pump.fun",
                "liquidity_usd": 1_000.0,
                "liquidity": 1_000.0,
                "market_cap": 5_000.0,
                "marketcap": 5_000.0,
                "price_usd": 0.000005,
                "price": 0.000005,
                "top_10_holder_rate": 0.5,
                "top10_holder_rate": 0.5,
                "symbol": "FAIL",
                "name": "Fail Initial Filter",
            },
            "FAIL_SECOND": {
                **self._token("FAIL_SECOND"),
                "price_usd": 0.00002,
                "price": 0.00002,
                "market_cap": 20_000.0,
                "marketcap": 20_000.0,
            },
        }

        self.latest = {
            "PASS1": self._latest(1.5),
            "PASS1_150": self._latest(1.5),
            "PASS1_510": self._latest(1.5),
            "FAIL_SECOND": self._latest(1.0),
            "FAIL_INIT": self._latest(1.0),
        }

        self.klines = {}
        self._refresh_all()

    def _token(self, token_mint: str) -> Dict[str, Any]:
        return {
            "token_mint": token_mint,
            "address": token_mint,
            "token_address": token_mint,
            "pool_address": f"POOL_{token_mint}",
            "pair_address": f"POOL_{token_mint}",
            **dict(self._base_token_def),
        }

    @staticmethod
    def _latest(price: float) -> Dict[str, Any]:
        return {
            "price": price,
            "calls": 0,
            "price_usd": price,
            "price_sol": price,
            "sol_price": price,
            "liquidity_usd": 20_000.0,
            "sol_side_liquidity": 1_000.0,
            "sol_liquidity": 1_000.0,
            "market_cap": 85_000.0,
        }

    def _refresh_all(self):
        now = datetime.now(timezone.utc)
        self._last_refresh = now
        pool_ages = {
            "PASS1": 150,
            "PASS1_150": 150,
            "PASS1_510": 510,
            "FAIL_INIT": 150,
            "FAIL_SECOND": 150,
        }
        for mint, token in self.tokens.items():
            age = pool_ages.get(mint, 150)
            created_at = now - timedelta(seconds=age)
            token["pool_created_at"] = created_at.isoformat()
            token["creation_timestamp"] = int(created_at.timestamp())
            token["pool_creation_timestamp"] = int(created_at.timestamp())

        self.klines = {
            "PASS1": self._make_klines(now, [1.0, 1.5, 2.0]),
            "PASS1_150": self._make_klines(now, [1.0, 1.5, 2.0]),
            "PASS1_510": [
                {"open_time": (now - timedelta(minutes=9)).isoformat(), "timestamp": int((now - timedelta(minutes=9)).timestamp()), "close": 1.0, "c": 1.0, "volume_usd": 1000.0},
                {"open_time": (now - timedelta(minutes=6)).isoformat(), "timestamp": int((now - timedelta(minutes=6)).timestamp()), "close": 1.5, "c": 1.5, "volume_usd": 1300.0},
                {"open_time": (now - timedelta(minutes=3)).isoformat(), "timestamp": int((now - timedelta(minutes=3)).timestamp()), "close": 2.0, "c": 2.0, "volume_usd": 1600.0},
            ],
            "FAIL_SECOND": self._make_klines(now, [1.0, 1.0]),
        }

    @staticmethod
    def _make_klines(now: datetime, closes):
        rows = []
        for idx, close in enumerate(closes):
            ts = now - timedelta(minutes=5 - idx)
            rows.append({
                "open_time": ts.isoformat(),
                "timestamp": int(ts.timestamp()),
                "open": close * 0.95,
                "o": close * 0.95,
                "high": close * 1.05,
                "h": close * 1.05,
                "low": close * 0.9,
                "l": close * 0.9,
                "close": close,
                "c": close,
                "volume_usd": 1000.0 + idx * 100,
                "volume": 1000.0 + idx * 100,
            })
        return rows

    def _maybe_refresh(self):
        now = datetime.now(timezone.utc)
        if now - self._last_refresh > self._refresh_interval:
            self._refresh_all()

    def advance_price(self, token_mint: str, delta: float):
        if token_mint in self.latest:
            self.latest[token_mint]["price"] = float(self.latest[token_mint].get("price", 0.0)) + delta
            self.latest[token_mint]["price_usd"] = self.latest[token_mint]["price"]
            self.latest[token_mint]["price_sol"] = self.latest[token_mint]["price"]
