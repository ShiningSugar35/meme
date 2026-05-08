from ..db.repositories import Repositories
from ..strategy.exit_rules import decide_exit
from ..strategy.filters import run_initial_filter
from datetime import datetime, timezone


class PositionRiskRunner:
    def __init__(self, repo: Repositories, gmgn):
        self.repo = repo
        self.gmgn = gmgn

    async def run_once(self):
        positions = await self.repo.list_open_positions()
        now = datetime.now(timezone.utc)
        for p in positions:
            token = p['token_mint']
            latest = await self.gmgn.fetch_latest_price(token)
            # build rolling_60s simplistic
            ticks = await self.repo.get_recent_ticks(token, 60)
            low = min([t.get('price_sol', latest.get('price')) for t in ticks]) if ticks else latest.get('price')
            high = max([t.get('price_sol', latest.get('price')) for t in ticks]) if ticks else latest.get('price')
            rolling = {'low': low, 'high': high}
            tick = {'price_sol': latest.get('price')}
            # risk recheck via filters using locked_strategy_config_json
            locked = p.get('locked_strategy_config_json')
            if locked:
                # call filters reuse
                import json
                try:
                    cfg = json.loads(locked)
                except json.JSONDecodeError:
                    # old data with str(dict), skip
                    await self.repo.append_system_event('WARN', 'RISK', 'Skipping risk recheck for old position with invalid config', str({'position_id': p['id']}))
                    continue
                snap = await self.gmgn.fetch_token_snapshot(token)
                res = await run_initial_filter(snap, cfg, now)
                if not res.passed:
                    # force exit
                    await self.repo.insert_strategy_match(token, cfg.get('id', 0), cfg.get('config_version', 1), None, 'risk_recheck', False, str([d.__dict__ for d in res.details]), str(res.feature_vector))
                    # append exit reason and close position
                    await self.repo.append_system_event('WARN', 'RISK', 'Risk recheck failed, closing', str({'token': token}))
                    await self.repo.close_position(p['id'], close_reason='RISK_RECHECK_FAILED', total_return_sol=0)
                    continue

            decision = await decide_exit(p, tick, rolling, {})
            if decision.should_exit:
                # simplified sell pipeline: mark trade event and update position
                await self.repo.append_trade_event(f"SELL:{token}:{p['id']}:1", token_mint=token, side='SELL', event_type='SELL', status='CONFIRMED')
                # update remaining to 0 if exit_pct ==1
                if decision.exit_pct >= 1.0:
                    await self.repo.close_position(p['id'], close_reason='EXIT', total_return_sol=0)
                else:
                    # partial exit: reduce remaining
                    await self.repo.update_position_remaining(p['id'], p.get('remaining_token_amount', 1.0) * (1 - decision.exit_pct), p.get('remaining_value_usd', 1.0) * (1 - decision.exit_pct))
