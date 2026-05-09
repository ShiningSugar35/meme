import asyncio
from typing import List, Dict, Any, Optional
import json
from ..db.repositories import Repositories
from ..strategy.sizing import compute_entry_size
from ..strategy.slippage import compute_slippage_bps
from ..logging_config import logger
from ..providers.base import SwapProvider, ExecutionProvider, RpcProvider, MarketDataProvider
from ..config import settings
from datetime import datetime, timezone
import math


WRAPPED_SOL_MINT = 'So11111111111111111111111111111111111111112'
LAMPORTS_PER_SOL = 1_000_000_000


class TradingPipeline:
    def __init__(self, repo: Repositories, gmgn: MarketDataProvider, jupiter: SwapProvider, jito: ExecutionProvider, rpc: RpcProvider):
        self.repo = repo
        self.gmgn = gmgn
        self.jupiter = jupiter
        self.jito = jito
        self.rpc = rpc

    def _safety_gate(self) -> Optional[Dict[str, Any]]:
        mode = settings.get_provider_mode()
        from ..config import ProviderMode
        if mode == ProviderMode.MOCK:
            return None
        if settings.DRY_RUN:
            return {'ok': False, 'error': 'DRY_RUN', 'message': 'DRY_RUN=true blocks real trade broadcasts'}
        if not settings.LIVE_TRADING_ENABLED:
            return {'ok': False, 'error': 'LIVE_TRADING_DISABLED', 'message': 'LIVE_TRADING_ENABLED=false'}
        if not settings.JITO_ENABLED:
            return {'ok': False, 'error': 'JITO_DISABLED', 'message': 'Jito is disabled, no RPC fallback allowed'}
        return None

    def _build_idempotency_key(self, side: str, token_mint: str, strategy: Dict[str, Any], snapshot_id: Optional[int], extra: str = '') -> str:
        sid = strategy.get('id', 0)
        ver = strategy.get('config_version', 1)
        sn = snapshot_id or 0
        return f"{side}:{token_mint}:{sid}:{ver}:{sn}{extra}"

    def _round_timestamp_bucket(self, bucket_seconds: int = 30) -> str:
        ts = int(datetime.now(timezone.utc).timestamp())
        return str((ts // bucket_seconds) * bucket_seconds)

    async def handle_token_second_filter_result(
        self, token_mint: str, passed_strategies: List[Dict[str, Any]],
        snapshot_id: Optional[int] = None, discovery_event_id: Optional[int] = None
    ):
        if snapshot_id is not None:
            existing = await self.repo.get_discovery_event_by_snapshot_token_pool(snapshot_id, token_mint, None)
            if existing:
                await self.repo.append_system_event('INFO', 'TRADE', 'duplicate snapshot skipped',
                    str({'token': token_mint, 'snapshot_id': snapshot_id, 'existing_id': existing.get('id')}))
                return {'status': 'SKIPPED_DUPLICATE_SNAPSHOT', 'token_mint': token_mint, 'discovery_event_id': existing.get('id')}

        if discovery_event_id is None:
            discovery_event_id, _ = await self.repo.create_discovery_event_idempotent(token_mint=token_mint, snapshot_id=snapshot_id)

        live_strategies = [s for s in passed_strategies if s.get('is_live')]
        sim_strategies = [s for s in passed_strategies if not s.get('is_live')]

        winner = None
        if live_strategies:
            winner = sorted(live_strategies, key=lambda s: (s.get('priority', 100), s.get('id', 0)))[0]

        existing_live = await self.repo.get_open_live_position_by_token(token_mint)
        if existing_live and winner:
            await self.repo.append_system_event('WARN', 'TRADE', 'Token already has open live position',
                str({'token': token_mint, 'existing_position_id': existing_live.get('id')}))
        elif winner:
            await self._execute_buy(token_mint, winner, snapshot_id, discovery_event_id)

        for s in passed_strategies:
            if s == winner:
                continue
            await self._create_simulated_position(token_mint, s, snapshot_id, discovery_event_id)

    async def _execute_buy(
        self, token_mint: str, strategy: Dict[str, Any],
        snapshot_id: Optional[int] = None, discovery_event_id: Optional[int] = None
    ):
        sid = strategy.get('id', 0)
        gate = self._safety_gate()
        if gate:
            await self.repo.append_system_event('WARN', 'TRADE', 'Buy blocked by safety gate', str({'token': token_mint, 'reason': gate['error']}))
            return

        latest = await self.gmgn.fetch_latest_price(token_mint)
        sol_liq = latest.get('sol_side_liquidity', 0)
        sizing = await compute_entry_size(sol_liq)
        if sizing.get('blocked'):
            await self.repo.append_system_event('WARN', 'TRADE', 'Buy blocked: liquidity', str({'token': token_mint}))
            return
        size_sol = sizing['size_sol']

        slippage = strategy.get('buy_slippage_cap_bps', 1500)
        quote = await self._get_quote(token_mint, int(size_sol * LAMPORTS_PER_SOL), slippage, sid)
        if quote.get('error'):
            await self.repo.append_system_event('ERROR', 'TRADE', 'Quote blocked', str({'token': token_mint, 'error': quote.get('error')}))
            return

        idem_pending = self._build_idempotency_key('BUY', token_mint, strategy, snapshot_id)
        te_pending = await self.repo.append_trade_event(
            idem_pending, token_mint=token_mint, side='BUY', event_type='BUY_SUBMITTED', status='SUBMITTED',
            is_live=1, requested_pct=100, requested_sol_amount=size_sol,
            price_usd=latest.get('price'), price_sol=latest.get('price_sol'),
            slippage_bps=slippage, price_impact_pct=quote.get('priceImpactPct'),
            quote_json=json.dumps({'impact': quote.get('priceImpactPct')}),
            route_plan_json=json.dumps(quote.get('routePlan', [])[:3]),
        )

        wallet_pubkey = settings.WALLET_PUBLIC_KEY or 'MOCK_PUBKEY'
        instr = await self.jupiter.build_swap_instructions(quote, wallet_pubkey, {})
        if instr.get('swapTransaction') is None and instr.get('mode') not in ('MOCK_NO_TRANSACTION', 'ONLINE_READONLY_NO_TRANSACTION'):
            await self.repo.append_system_event('ERROR', 'TRADE', 'build_swap failed', str({'token': token_mint}))
            return

        sim = await self.jito.simulate(instr)
        if not sim.get('ok'):
            await self.repo.append_system_event('ERROR', 'JITO_SIM', 'Simulate failed', str(sim))
            return

        send_result = await self.jito.send(instr)
        if not send_result.get('ok'):
            await self.repo.append_system_event('ERROR', 'JITO_SEND', 'Send failed', str(send_result))
            return

        sig = send_result.get('signature') or send_result.get('bundle_id', '')
        try:
            confirmation = await asyncio.wait_for(self.rpc.wait_signature(sig, 30), timeout=31)
        except asyncio.TimeoutError:
            await self.repo.append_system_event('WARN', 'TRADE', 'Confirmation timeout', str({'sig': sig[:20]}))
            confirmation = {'status': 'timeout'}

        idem_confirmed = self._build_idempotency_key('BUY_CFM', token_mint, strategy, snapshot_id)
        te_confirmed = await self.repo.append_trade_event(
            idem_confirmed, token_mint=token_mint, side='BUY', event_type='BUY_CONFIRMED', status='CONFIRMED',
            is_live=1, requested_pct=100, requested_sol_amount=size_sol,
            executed_sol_amount=size_sol, executed_token_amount=quote.get('outAmount'),
            price_usd=latest.get('price'), price_sol=latest.get('price_sol'),
            slippage_bps=slippage, price_impact_pct=quote.get('priceImpactPct'),
            tx_signature=sig, bundle_id=send_result.get('bundle_id'),
            provider='JITO'
        )

        opened_at = datetime.now(timezone.utc).isoformat()
        token_amount = float(quote.get('outAmount', size_sol)) / LAMPORTS_PER_SOL
        pos_id = await self.repo.create_position(
            token_mint, True, json.dumps(strategy), 'POSITION_OPEN',
            latest.get('price'), latest.get('price_sol'),
            token_amount, token_amount, size_sol * (latest.get('price_usd') or latest.get('price', 0)),
            opened_at, live_strategy_id=sid, strategy_config_version=strategy.get('config_version', 1),
            total_cost_sol=size_sol, open_trade_event_id=te_confirmed.get('id'),
            last_fill_at=opened_at, last_fill_price_usd=latest.get('price'),
            discovery_event_id=discovery_event_id,
        )

        await self.repo.insert_bandit_observation(
            token_mint, sid, True, json.dumps({'entry_price': latest.get('price'), 'size_sol': size_sol}),
            json.dumps(strategy), position_id=pos_id, discovery_event_id=discovery_event_id
        )
        await self.repo.insert_strategy_match(token_mint, sid, strategy.get('config_version', 1), snapshot_id,
            'live_executed', True, '{}', '{}', discovery_event_id=discovery_event_id)
        if discovery_event_id:
            await self.repo.update_discovery_event_status(discovery_event_id, 'LIVE_POSITION_OPEN')

        await self.repo.append_system_event('INFO', 'TRADE', 'Buy executed',
            str({'token': token_mint, 'position_id': pos_id}))

    async def execute_sell(
        self, position: Dict[str, Any], exit_pct: float = 1.0, exit_reason: str = 'EXIT'
    ) -> Optional[Dict[str, Any]]:
        gate = self._safety_gate()
        if gate:
            await self.repo.append_system_event('WARN', 'TRADE', 'Sell blocked by safety gate',
                str({'position_id': position.get('id'), 'reason': gate['error']}))
            return gate

        pos_id = position['id']
        token_mint = position['token_mint']
        remaining_token = position.get('remaining_token_amount', 0)
        if remaining_token <= 0:
            await self.repo.append_system_event('WARN', 'TRADE', 'Sell blocked: zero remaining',
                str({'position_id': pos_id}))
            return {'ok': False, 'error': 'ZERO_REMAINING'}

        sell_amount = remaining_token * exit_pct
        slippage = position.get('locked_strategy_config_json')
        sell_bps = 2000
        if slippage:
            try:
                cfg = json.loads(slippage)
                sell_bps = cfg.get('sell_slippage_cap_bps', 2000)
            except (json.JSONDecodeError, TypeError):
                pass

        quote = await self._get_quote(token_mint, int(sell_amount * LAMPORTS_PER_SOL), sell_bps, pos_id, is_sell=True)
        if quote.get('error'):
            await self.repo.append_system_event('ERROR', 'TRADE', 'Sell quote blocked',
                str({'position_id': pos_id, 'error': quote.get('error')}))
            return {'ok': False, 'error': quote.get('error')}

        bucket = self._round_timestamp_bucket()
        idem_pending = f"SELL:{pos_id}:{exit_reason}:{bucket}"
        te_pending = await self.repo.append_trade_event(
            idem_pending, position_id=pos_id, token_mint=token_mint,
            side='SELL', event_type='SELL_PENDING', status='PENDING', is_live=1,
            requested_pct=exit_pct * 100,
            requested_token_amount=sell_amount,
            requested_sol_amount=float(quote.get('outAmount', 0)) / LAMPORTS_PER_SOL,
            price_usd=position.get('entry_price_usd'),
            price_sol=position.get('entry_price_sol'),
            slippage_bps=sell_bps,
            price_impact_pct=quote.get('priceImpactPct'),
            quote_json=json.dumps({'impact': quote.get('priceImpactPct')}),
        )

        wallet_pubkey = settings.WALLET_PUBLIC_KEY or 'MOCK_PUBKEY'
        instr = await self.jupiter.build_swap_instructions(quote, wallet_pubkey, {})
        if instr.get('swapTransaction') is None and instr.get('mode') not in ('MOCK_NO_TRANSACTION', 'ONLINE_READONLY_NO_TRANSACTION'):
            return {'ok': False, 'error': 'BUILD_FAILED'}

        send_result = await self.jito.send(instr)
        if not send_result.get('ok'):
            return send_result

        sig = send_result.get('signature') or send_result.get('bundle_id', '')
        try:
            await asyncio.wait_for(self.rpc.wait_signature(sig, 30), timeout=31)
        except asyncio.TimeoutError:
            pass

        idem_cfm = f"SELL_CFM:{pos_id}:{exit_reason}:{bucket}"
        te_confirmed = await self.repo.append_trade_event(
            idem_cfm, position_id=pos_id, token_mint=token_mint,
            side='SELL', event_type='SELL_CONFIRMED', status='CONFIRMED', is_live=1,
            requested_pct=exit_pct * 100,
            executed_sol_amount=float(quote.get('outAmount', 0)) / LAMPORTS_PER_SOL,
            executed_token_amount=sell_amount,
            tx_signature=sig, bundle_id=send_result.get('bundle_id'),
            slippage_bps=sell_bps, price_impact_pct=quote.get('priceImpactPct'),
            provider='JITO'
        )

        if exit_pct >= 1.0:
            await self.repo.close_position(pos_id, close_reason=exit_reason,
                total_return_sol=float(quote.get('outAmount', 0)) / LAMPORTS_PER_SOL)
        else:
            new_remaining = remaining_token * (1 - exit_pct)
            new_value = position.get('remaining_value_usd', 0) * (1 - exit_pct)
            await self.repo.update_position_remaining(pos_id, new_remaining, new_value)

        await self.repo.append_system_event('INFO', 'TRADE', 'Sell executed',
            str({'position_id': pos_id, 'exit_reason': exit_reason, 'exit_pct': exit_pct}))
        return {'ok': True, 'position_id': pos_id}

    async def _get_quote(self, token_mint: str, amount_lamports: int, slippage_bps: int, strategy_id: int, is_sell: bool = False) -> Dict[str, Any]:
        input_mint, output_mint = (token_mint, WRAPPED_SOL_MINT) if is_sell else (WRAPPED_SOL_MINT, token_mint)
        quote = await self.jupiter.quote_exact_in(input_mint, output_mint, amount_lamports, slippage_bps)
        cap = settings.PRICE_IMPACT_HARD_CAP_PCT / 100.0
        if quote.get('priceImpactPct', 0) > cap:
            quote['error'] = 'HIGH_PRICE_IMPACT'
        return quote

    async def _create_simulated_position(
        self, token_mint: str, strategy: Dict[str, Any],
        snapshot_id: Optional[int] = None, discovery_event_id: Optional[int] = None
    ):
        latest = await self.gmgn.fetch_latest_price(token_mint)
        sol_liq = latest.get('sol_side_liquidity')
        sizing = await compute_entry_size(sol_liq)
        size_sol = sizing.get('size_sol', 0)
        sid = strategy.get('id', 0)

        pos_id = await self.repo.create_position(
            token_mint, False, json.dumps(strategy), 'SIM_OPEN',
            latest.get('price'), latest.get('price_sol'),
            size_sol, size_sol, size_sol * latest.get('price_usd', 0),
            None, live_strategy_id=None, strategy_config_version=strategy.get('config_version', 1),
            total_cost_sol=0.0, open_trade_event_id=None,
            last_fill_at=None, last_fill_price_usd=None, discovery_event_id=discovery_event_id,
        )
        await self.repo.insert_bandit_observation(token_mint, sid, False,
            json.dumps({'entry_price': latest.get('price'), 'size_sol': size_sol}),
            json.dumps(strategy), position_id=pos_id, discovery_event_id=discovery_event_id)
        await self.repo.insert_strategy_match(token_mint, sid, strategy.get('config_version', 1),
            snapshot_id, 'simulated', True, '{}', '{}', discovery_event_id=discovery_event_id)
