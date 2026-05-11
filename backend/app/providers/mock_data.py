from datetime import datetime, timedelta, timezone

class MockData:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self._pool_base = now
        self._last_refresh = now
        self._refresh_interval = timedelta(seconds=30)

        # define 3 tokens
        # token A: passes initial and second filter
        self.tokens = {
            'PASS1': {
                'token_mint': 'PASS1',
                'type': 'new_creation',
                'liquidity_usd': 20000,
                'top_10_holder_rate': 0.18,
                'top1_holder_rate': 0.04,
                'renounced_mint': 1,
                'renounced_freeze_account': 1,
                'max_rug_ratio': -0.1,
                'max_insider_ratio': -0.1,
                'max_entrapment_ratio': -0.1,
                'is_wash_trading': 0,
                'rat_trader_amount_rate': -0.1,
                'suspected_insider_hold_rate': 0.01,
                'max_bundler_rate': -0.1,
                'fresh_wallet_rate': 0.05,
                'sell_tax': 0.0,
                'has_social': 1,
                'creator_token_status': 'creator_close',
                'dev_team_hold_rate': 0.0,
                'dev_token_burn_ratio': 1.0,
                'sniper_count': 1,
                'platform': 'Pump.fun',
            },
            'FAIL_INIT': {
                'token_mint': 'FAIL_INIT',
                'type': 'new_creation',
                'liquidity_usd': 1000,
                'top_10_holder_rate': 0.5,
                'platform': 'Pump.fun',
            },
            'FAIL_SECOND': {
                'token_mint': 'FAIL_SECOND',
                'type': 'new_creation',
                'liquidity_usd': 20000,
                'top_10_holder_rate': 0.18,
                'top1_holder_rate': 0.04,
                'renounced_mint': 1,
                'renounced_freeze_account': 1,
                'max_rug_ratio': -0.1,
                'max_insider_ratio': -0.1,
                'max_entrapment_ratio': -0.1,
                'is_wash_trading': 0,
                'rat_trader_amount_rate': -0.1,
                'suspected_insider_hold_rate': 0.01,
                'max_bundler_rate': -0.1,
                'fresh_wallet_rate': 0.05,
                'sell_tax': 0.0,
                'has_social': 1,
                'creator_token_status': 'creator_close',
                'dev_team_hold_rate': 0.0,
                'dev_token_burn_ratio': 1.0,
                'sniper_count': 1,
                'platform': 'Pump.fun',
            }
        }

        self.klines = {}

        # latest prices that can change over time; simple increments per call
        self.latest = {
            'PASS1': {'price': 1.5, 'calls': 0, 'price_usd': 1.5, 'sol_price': 1.5, 'sol_liquidity': 1000},
            'FAIL_SECOND': {'price': 1.0, 'calls': 0, 'price_usd': 1.0, 'sol_price': 1.0, 'sol_liquidity': 1000},
            'FAIL_INIT': {'price': 1.0, 'calls': 0, 'price_usd': 1.0, 'sol_price': 1.0, 'sol_liquidity': 1000},
        }

        self._refresh_all()

    def _refresh_all(self):
        """Refresh time-dependent data: pool_created_at and klines."""
        now = datetime.now(timezone.utc)
        self._last_refresh = now
        pool_age = now - timedelta(seconds=150)
        for mint, t in self.tokens.items():
            t['pool_created_at'] = pool_age.isoformat()
        self.klines = {
            'PASS1': [
                {'open_time': (now - timedelta(minutes=5)).isoformat(), 'close': 1.0},
                {'open_time': (now - timedelta(minutes=4)).isoformat(), 'close': 1.5},
                {'open_time': (now - timedelta(minutes=3)).isoformat(), 'close': 2.0},
            ],
            'FAIL_SECOND': [
                {'open_time': (now - timedelta(minutes=5)).isoformat(), 'close': 1.0},
                {'open_time': (now - timedelta(minutes=4)).isoformat(), 'close': 1.0},
            ]
        }

    def _maybe_refresh(self):
        """Refresh time-dependent data every 30 seconds so time windows stay valid."""
        now = datetime.now(timezone.utc)
        if now - self._last_refresh > self._refresh_interval:
            self._refresh_all()

    def advance_price(self, token_mint: str, delta: float):
        if token_mint in self.latest:
            self.latest[token_mint]['price'] += delta
