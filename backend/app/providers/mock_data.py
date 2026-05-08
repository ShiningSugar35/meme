from datetime import datetime, timedelta, timezone

class MockData:
    def __init__(self):
        now = datetime.now(timezone.utc)
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
                'pool_created_at': (now - timedelta(seconds=150)).isoformat(),
                'platform': 'Pump.fun',
            },
            'FAIL_INIT': {
                'token_mint': 'FAIL_INIT',
                'type': 'new_creation',
                'liquidity_usd': 1000,
                'top_10_holder_rate': 0.5,
                'pool_created_at': (now - timedelta(seconds=150)).isoformat(),
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
                'pool_created_at': (now - timedelta(seconds=150)).isoformat(),
                'platform': 'Pump.fun',
            }
        }

        # klines: for PASS1 good, for FAIL_SECOND bad
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

        # latest prices that can change over time; simple increments per call
        self.latest = {
            'PASS1': {'price': 1.0, 'calls': 0, 'price_usd': 1.0, 'sol_price': 1.0, 'sol_liquidity': 1000},
            'FAIL_SECOND': {'price': 1.0, 'calls': 0, 'price_usd': 1.0, 'sol_price': 1.0, 'sol_liquidity': 1000},
            'FAIL_INIT': {'price': 1.0, 'calls': 0, 'price_usd': 1.0, 'sol_price': 1.0, 'sol_liquidity': 1000},
        }

    def advance_price(self, token_mint: str, delta: float):
        if token_mint in self.latest:
            self.latest[token_mint]['price'] += delta
