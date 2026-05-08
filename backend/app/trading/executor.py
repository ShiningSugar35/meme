import asyncio
from typing import List, Dict, Any, Optional, Tuple
import json
from ..db.repositories import Repositories
from ..strategy.sizing import compute_entry_size
from ..strategy.slippage import compute_slippage_bps
from ..logging_config import logger
from ..providers.base import SwapProvider, ExecutionProvider, RpcProvider, MarketDataProvider
from datetime import datetime, timezone


class TradingPipeline:
    def __init__(self, repo: Repositories, gmgn: MarketDataProvider, jupiter: SwapProvider, jito: ExecutionProvider, rpc: RpcProvider):
        self.repo = repo
        self.gmgn = gmgn
        self.jupiter = jupiter
        self.jito = jito
        self.rpc = rpc

    async def handle_token_second_filter_result(
        self, token_mint: str, passed_strategies: List[Dict[str, Any]], 
        snapshot_id: Optional[int] = None, discovery_event_id: Optional[int] = None
    ):
        await self.repo.append_system_event('INFO', 'TRADE', 'handle_token_second_filter_result called', 
                                            str({'token': token_mint, 'passed_count': len(passed_strategies), 
                                                'snapshot_id': snapshot_id, 'discovery_event_id': discovery_event_id}))

        # ====== SNAPSHOT IDEMPOTENCY CHECK ======
        # If snapshot_id is provided, check if we already processed this snapshot+token+pool
        if snapshot_id is not None:
            existing_discovery = await self.repo.get_discovery_event_by_snapshot_token_pool(
                snapshot_id, token_mint, None  # pool_address is None at this point
            )
            if existing_discovery:
                # ALREADY PROCESSED - skip everything, return idempotent result
                await self.repo.append_system_event(
                    'INFO', 'TRADE', 'duplicate snapshot skipped',
                    str({
                        'token': token_mint,
                        'snapshot_id': snapshot_id,
                        'existing_discovery_event_id': existing_discovery.get('id'),
                        'status': 'SKIPPED_DUPLICATE_SNAPSHOT'
                    })
                )
                # Return structured result without doing anything
                return {
                    'status': 'SKIPPED_DUPLICATE_SNAPSHOT',
                    'token_mint': token_mint,
                    'snapshot_id': snapshot_id,
                    'discovery_event_id': existing_discovery.get('id'),
                    'live_executed': False,
                    'simulated_created': 0
                }

        # ====== GET OR CREATE DISCOVERY EVENT ======
        if discovery_event_id is None:
            # Create new discovery event (idempotent for same snapshot_id)
            discovery_event_id, created = await self.repo.create_discovery_event_idempotent(
                token_mint=token_mint,
                snapshot_id=snapshot_id
            )
            if created:
                await self.repo.append_system_event('INFO', 'TRADE', 'Created discovery event', 
                                                    str({'id': discovery_event_id, 'created': True}))
            else:
                await self.repo.append_system_event('INFO', 'TRADE', 'Reusing existing discovery event', 
                                                    str({'id': discovery_event_id, 'created': False}))

        # ====== SEPARATE LIVE AND SIM STRATEGIES ======
        live_strategies = [s for s in passed_strategies if s.get('is_live')]
        sim_strategies = [s for s in passed_strategies if not s.get('is_live')]
        await self.repo.append_system_event('INFO', 'TRADE', 'live_strategies', 
                                            str({'count': len(live_strategies), 'live': live_strategies}))

        # ====== CHOOSE WINNER LIVE STRATEGY ======
        winner = None
        if live_strategies:
            live_strategies_sorted = sorted(live_strategies, key=lambda s: (s.get('priority', 100), s.get('id', 0)))
            winner = live_strategies_sorted[0]
            await self.repo.append_system_event('INFO', 'TRADE', 'winner selected', 
                                                    str({'winner_id': winner.get('id'), 'priority': winner.get('priority')}))

        # ====== CHECK IF TOKEN ALREADY HAS OPEN LIVE POSITION IN THIS CYCLE ======
        # Note: closed positions do NOT block new trades in a new cycle
        existing_live = await self.repo.get_open_live_position_by_token_and_cycle(token_mint, discovery_event_id)
        if existing_live:
            await self.repo.append_system_event('WARN', 'TRADE', 'Token already has open live position in this cycle, blocking duplicate', 
                                                        str({'token': token_mint, 'discovery_event_id': discovery_event_id}))
            # still process sim strategies
        else:
            if winner:
                await self.repo.append_system_event('INFO', 'TRADE', 'About to call _execute_live_buy', 
                                                        str({'token': token_mint, 'strategy_id': winner.get('id')}))
                await self._execute_live_buy(token_mint, winner, snapshot_id, discovery_event_id)

        # ====== CREATE SIMULATED POSITIONS/OBSERVATIONS ======
        for s in passed_strategies:
            if s == winner:
                continue
            await self._create_simulated_position(token_mint, s, snapshot_id, discovery_event_id)

    async def _execute_live_buy(
        self, token_mint: str, strategy: Dict[str, Any], 
        snapshot_id: Optional[int] = None, discovery_event_id: Optional[int] = None
    ):
        await self.repo.append_system_event('INFO', 'TRADE', 'executing live buy', 
                                                str({'token': token_mint, 'strategy_id': strategy.get('id'), 
                                                    'discovery_event_id': discovery_event_id}))

        # get latest price for sol_side_liquidity
        latest = await self.gmgn.fetch_latest_price(token_mint)
        await self.repo.append_system_event('DEBUG', 'TRADE', 'latest price', str(latest))
        sol_liq = latest.get('sol_side_liquidity')
        sizing = await compute_entry_size(sol_liq)
        await self.repo.append_system_event('DEBUG', 'TRADE', 'sizing result', str(sizing))
        if sizing.get('blocked'):
            await self.repo.append_system_event('WARN', 'TRADE', 'Live buy blocked: sol_side_liquidity missing', str({'token': token_mint}))
            return
        size_sol = sizing['size_sol']

        # quote
        quote = await self.jupiter.quote_exact_in('SOL', token_mint, int(size_sol * 1_000_000_000), strategy.get('buy_slippage_cap_bps', 1500))
        if quote.get('priceImpactPct', 0) > 0.10:
            await self.repo.append_system_event('ERROR', 'JUPITER', 'Price impact too high', 
                                                        str({'token': token_mint, 'impact': quote.get('priceImpactPct')}))
            return

        # build
        instr = await self.jupiter.build_swap_instructions(quote, 'MOCK_PUBKEY', {})

        # simulate with retry logic (tip ladder)
        tip_ladder = [0.50, 0.75, 0.95]  # percentiles
        # For mock, we just simulate once; real retry would adjust tip.
        sim = await self.jito.simulate(instr)
        if not sim.get('ok'):
            await self.repo.append_system_event('ERROR', 'JITO_SIM', 'Simulate failed', str(sim))
            return

        # send with retry
        send_result = await self.jito.send(instr)
        if not send_result.get('ok'):
            await self.repo.append_system_event('ERROR', 'JITO_SEND', 'Send failed', str(send_result))
            return

        # record a submitted trade event (idempotent)
        idempotency_submitted = f"LIVE_BUY_SUBMITTED:{token_mint}:{strategy.get('id')}:{snapshot_id or 0}:{discovery_event_id or 0}"
        te_sub = await self.repo.append_trade_event(
            idempotency_submitted,
            token_mint=token_mint,
            side='BUY',
            event_type='BUY_SUBMITTED',
            status='SUBMITTED',
            is_live=1,
            tx_signature=send_result.get('signature'),
            bundle_id=send_result.get('bundle_id'),
        )

        # wait for confirmation
        await self.rpc.wait_signature(send_result.get('signature') or send_result.get('bundle_id'), 30)

        # record confirmed trade event
        idempotency_confirmed = f"LIVE_BUY_CONFIRMED:{token_mint}:{strategy.get('id')}:{snapshot_id or 0}:{discovery_event_id or 0}"
        te_conf = await self.repo.append_trade_event(
            idempotency_confirmed,
            token_mint=token_mint,
            side='BUY',
            event_type='BUY_CONFIRMED',
            status='CONFIRMED',
            is_live=1,
            tx_signature=send_result.get('signature'),
            bundle_id=send_result.get('bundle_id'),
        )

        # create position with proper linkage to trade event and strategy
        opened_at = datetime.now(timezone.utc).isoformat()
        pos_id = await self.repo.create_position(
            token_mint,
            True,
            json.dumps(strategy),
            'POSITION_OPEN',
            latest.get('price'),
            latest.get('price_sol'),
            size_sol,
            size_sol,
            size_sol * latest.get('price_usd', 0),
            opened_at,
            live_strategy_id=strategy.get('id'),
            strategy_config_version=strategy.get('config_version', 1),
            total_cost_sol=size_sol,
            open_trade_event_id=te_conf.get('id'),
            last_fill_at=opened_at,
            last_fill_price_usd=latest.get('price'),
            discovery_event_id=discovery_event_id,
        )

        # record bandit observation with position linkage and entry details
        action_json = json.dumps({'entry_price_usd': latest.get('price'), 'entry_size_sol': size_sol})
        await self.repo.insert_bandit_observation(token_mint, strategy.get('id'), True, action_json, '{}', 
                                                   position_id=pos_id, discovery_event_id=discovery_event_id)

        # record strategy match for live winner
        await self.repo.insert_strategy_match(token_mint, strategy.get('id'), strategy.get('config_version', 1), 
                                                   snapshot_id, 'live_executed', True, '{}', '{}', 
                                                   discovery_event_id=discovery_event_id)

        # Update discovery event status
        if discovery_event_id:
            await self.repo.update_discovery_event_status(discovery_event_id, 'LIVE_POSITION_OPEN')

        await self.repo.append_system_event('INFO', 'TRADE', 'Live buy executed', 
                                                str({'token': token_mint, 'position_id': pos_id, 
                                                    'discovery_event_id': discovery_event_id}))

    async def _create_simulated_position(
        self, token_mint: str, strategy: Dict[str, Any], 
        snapshot_id: Optional[int] = None, discovery_event_id: Optional[int] = None
    ):
        # simulated position: use same quote but not send
        latest = await self.gmgn.fetch_latest_price(token_mint)
        sol_liq = latest.get('sol_side_liquidity')
        sizing = await compute_entry_size(sol_liq)
        size_sol = sizing.get('size_sol', 0)

        # create simulated position
        pos_id = await self.repo.create_position(
            token_mint,
            False,
            json.dumps(strategy),
            'SIM_OPEN',
            latest.get('price'),
            latest.get('price_sol'),
            size_sol,
            size_sol,
            size_sol * latest.get('price_usd', 0),
            None,
            live_strategy_id=None,
            strategy_config_version=strategy.get('config_version', 1),
            total_cost_sol=0.0,
            open_trade_event_id=None,
            last_fill_at=None,
            last_fill_price_usd=None,
            discovery_event_id=discovery_event_id,
        )

        # record bandit observation linked to simulated position
        action_json = json.dumps({'entry_price_usd': latest.get('price'), 'entry_size_sol': size_sol})
        await self.repo.insert_bandit_observation(token_mint, strategy.get('id'), False, action_json, '{}', 
                                                   position_id=pos_id, discovery_event_id=discovery_event_id)

        # record strategy match if not already
        await self.repo.insert_strategy_match(token_mint, strategy.get('id'), strategy.get('config_version', 1), 
                                               snapshot_id, 'simulated', True, '{}', '{}', 
                                               discovery_event_id=discovery_event_id)

        await self.repo.append_system_event('INFO', 'SIM', 'Simulated position created', 
                                                str({'token': token_mint, 'strategy_id': strategy.get('id'), 
                                                    'discovery_event_id': discovery_event_id}))
