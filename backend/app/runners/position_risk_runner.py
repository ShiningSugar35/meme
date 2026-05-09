from ..db.repositories import Repositories
from ..strategy.exit_rules import decide_exit
from ..strategy.filters import run_initial_filter
from ..services.event_bus import event_bus
from ..config import settings
from datetime import datetime, timezone
from typing import Dict
from ..logging_config import logger


class PositionRiskRunner:
    def __init__(self, repo: Repositories, gmgn):
        self.repo = repo
        self.gmgn = gmgn
        self._last_scan: Dict[int, float] = {}

    async def run_once(self):
        positions = await self.repo.list_open_positions()
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()

        for p in positions:
            token = p['token_mint']
            pos_id = p['id']

            remaining_token = p.get('remaining_token_amount', 0) or 0
            entry_price_sol = p.get('entry_price_sol', 0) or 0
            remaining_value_sol = remaining_token * entry_price_sol

            interval = settings.get_risk_scan_interval_seconds(remaining_value_sol)
            last = self._last_scan.get(pos_id, 0)
            if last > 0 and (now_ts - last) < interval:
                continue

            self._last_scan[pos_id] = now_ts

            if remaining_value_sol < settings.DUST_FORCE_EXIT_SOL:
                await self.repo.append_system_event(
                    'WARN', 'RISK', 'Dust position force exit',
                    str({'position_id': pos_id, 'token': token, 'remaining_value_sol': remaining_value_sol})
                )
                await self.repo.append_trade_event(
                    f"SELL_DUST:{token}:{pos_id}", token_mint=token,
                    side='SELL', event_type='SELL', status='CONFIRMED'
                )
                await self.repo.close_position(pos_id, close_reason='DUST_FORCE_EXIT', total_return_sol=0)
                await event_bus.publish('system', {
                    'level': 'WARN', 'category': 'RISK',
                    'message': f'Dust force exit for {token}'
                })
                continue

            try:
                latest = await self.gmgn.fetch_latest_price(token)
            except Exception as e:
                await self.repo.append_system_event('ERROR', 'RISK', f'GMGN latest failed for {token}', str({'error': str(e)}))
                continue

            ticks = await self.repo.get_recent_ticks(token, 120)
            low = min([t.get('price_sol', latest.get('price')) for t in ticks]) if ticks else latest.get('price')
            high = max([t.get('price_sol', latest.get('price')) for t in ticks]) if ticks else latest.get('price')
            rolling = {'low': low, 'high': high}
            tick = {'price_sol': latest.get('price')}

            locked = p.get('locked_strategy_config_json')
            if locked:
                import json
                try:
                    cfg = json.loads(locked)
                except json.JSONDecodeError:
                    await self.repo.append_system_event('WARN', 'RISK', 'Skipping risk recheck for old position', str({'position_id': pos_id}))
                    continue
                snap = await self.gmgn.fetch_token_snapshot(token)
                res = await run_initial_filter(snap, cfg, now)
                if not res.passed:
                    await self.repo.insert_strategy_match(
                        token, cfg.get('id', 0), cfg.get('config_version', 1), None,
                        'risk_recheck', False,
                        str([d.__dict__ for d in res.details]), str(res.feature_vector)
                    )
                    await self.repo.append_system_event('WARN', 'RISK', 'Risk recheck failed, closing', str({'token': token}))
                    await self.repo.close_position(pos_id, close_reason='RISK_RECHECK_FAILED', total_return_sol=0)
                    await event_bus.publish('system', {'level': 'WARN', 'category': 'RISK', 'message': f'Risk recheck failed for {token}'})
                    continue

            decision = await decide_exit(p, tick, rolling, {})
            if decision.should_exit:
                await self.repo.append_trade_event(
                    f"SELL:{token}:{pos_id}:1", token_mint=token,
                    side='SELL', event_type='SELL', status='CONFIRMED'
                )
                if decision.exit_pct >= 1.0:
                    await self.repo.close_position(pos_id, close_reason='EXIT', total_return_sol=0)
                    await event_bus.publish('system', {'level': 'INFO', 'category': 'RISK', 'message': f'Full exit for {token}'})
                else:
                    new_remaining = (remaining_token or 1.0) * (1 - decision.exit_pct)
                    new_value = (p.get('remaining_value_usd', 0) or 0) * (1 - decision.exit_pct)
                    await self.repo.update_position_remaining(pos_id, new_remaining, new_value)
                    await event_bus.publish('system', {'level': 'INFO', 'category': 'RISK', 'message': f'Partial exit ({decision.exit_pct:.0%}) for {token}'})
