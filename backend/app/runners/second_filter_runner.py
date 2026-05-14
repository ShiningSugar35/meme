import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger
from ..providers.base import ExecutionProvider, MarketDataProvider, RpcProvider, SwapProvider
from ..strategy.filters import run_risk_filter
from ..strategy.second_filter import run_second_filter
from ..trading.executor import TradingPipeline

MOCK_MINTS = {'PASS1', 'PASS1_150', 'PASS1_510', 'FAIL_INIT', 'FAIL_SECOND'}

SNAPSHOT_COLUMNS = [
    'type',
    'liquidity_usd',
    'sol_side_liquidity',
    'volume_usd',
    'market_cap',
    'price_usd',
    'price_sol',
    'top_10_holder_rate',
    'top1_holder_rate',
    'renounced_mint',
    'renounced_freeze_account',
    'max_rug_ratio',
    'max_insider_ratio',
    'max_entrapment_ratio',
    'is_wash_trading',
    'rat_trader_amount_rate',
    'suspected_insider_hold_rate',
    'max_bundler_rate',
    'fresh_wallet_rate',
    'sell_tax',
    'has_social',
    'creator_token_status',
    'dev_team_hold_rate',
    'dev_token_burn_ratio',
    'sniper_count',
    'source_mode',
]


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(',', ':'))


def _snapshot_kwargs(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {k: snapshot.get(k) for k in SNAPSHOT_COLUMNS if snapshot.get(k) is not None}


def _first_present(data: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != '':
            return value
    return default


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _kline_time(kline: Dict[str, Any]) -> str:
    return str(_first_present(kline, ['open_time', 'time', 'timestamp', 't'], default=''))


def _sort_klines(klines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(klines, key=_kline_time)


def _extract_buy_sell_1m(klines: List[Dict[str, Any]], snapshot: Dict[str, Any]) -> Dict[str, float]:
    latest_kline = _sort_klines(klines)[-1] if klines else {}
    buy = _to_float(_first_present(latest_kline, ['buy_volume', 'buyVolume', 'buy_vol', 'buyVol']))
    sell = _to_float(_first_present(latest_kline, ['sell_volume', 'sellVolume', 'sell_vol', 'sellVol']))

    if buy is None:
        buy = _to_float(_first_present(snapshot, ['buy_volume_1m', 'buy_1m', 'buy_volume']))
    if sell is None:
        sell = _to_float(_first_present(snapshot, ['sell_volume_1m', 'sell_1m', 'sell_volume']))

    return {
        'buy_volume': float(buy or 0.0),
        'sell_volume': float(sell or 0.0),
    }


class SecondFilterRunner:
    def __init__(
        self,
        repo: Repositories,
        gmgn: MarketDataProvider,
        jupiter: SwapProvider,
        jito: ExecutionProvider,
        rpc: RpcProvider,
        strategy_groups: List[dict],
    ):
        self.repo = repo
        self.gmgn = gmgn
        self.jupiter = jupiter
        self.jito = jito
        self.rpc = rpc
        self.strategy_groups = strategy_groups or []
        self.pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)


    async def _load_enabled_strategy_groups(self) -> List[dict]:
        try:
            groups = await self.repo.get_enabled_strategy_groups()
        except Exception as e:
            logger.error(f"load enabled strategy groups for second filter failed: {e}")
            groups = self.strategy_groups or []

        try:
            runtime = await self.repo.get_all_runtime_settings()
            user_mode = runtime.get('user_mode', 'IDLE')
        except Exception:
            user_mode = 'IDLE'

        if user_mode == 'SIM_TEST':
            groups = [g for g in groups if not bool(g.get('is_live'))]
        elif user_mode == 'FORMAL_SIM_LIVE':
            groups = list(groups)
        else:
            groups = []

        self.strategy_groups = groups
        return groups

    async def _initial_passed_strategy_groups(self, discovery_event_id: int) -> List[Dict[str, Any]]:
        """Only re-check strategies that passed initial filter for this discovery event."""
        strategy_by_id = {int(s.get('id', 0)): s for s in self.strategy_groups}
        async with self.repo.db.execute(
            """
            SELECT DISTINCT strategy_id
            FROM token_strategy_matches
            WHERE discovery_event_id = ?
              AND stage = 'initial_filter'
              AND passed = 1
            ORDER BY strategy_id ASC
            """,
            (discovery_event_id,),
        ) as cur:
            rows = await cur.fetchall()

        groups = [strategy_by_id.get(int(row[0])) for row in rows if int(row[0]) in strategy_by_id]
        groups = [g for g in groups if g is not None]
        return groups

    async def run_once(self):
        now = datetime.now(timezone.utc)
        mode = settings.get_provider_mode()

        await self._load_enabled_strategy_groups()

        # 只处理初筛已经通过、等待二筛复核的池子。
        discovery_events = await self.repo.list_pending_second_filter_events(limit=200)

        for event in discovery_events:
            token_mint = event['token_mint']
            discovery_event_id = event['id']

            if mode != ProviderMode.MOCK and token_mint in MOCK_MINTS:
                continue

            token_row = await self.repo.get_token(token_mint) or {}
            try:
                fresh_snapshot = await self.gmgn.fetch_token_snapshot(token_mint)
                latest = await self.gmgn.fetch_latest_price(token_mint)
                klines = await self.gmgn.fetch_kline(token_mint, '1m', 5)
            except Exception as e:
                await self.repo.append_system_event(
                    'ERROR', 'SECOND_FILTER', 'GMGN second-filter fetch failed',
                    _json_dumps({'token': token_mint, 'discovery_event_id': discovery_event_id, 'error': str(e)}),
                    account_type='SIM',
                )
                continue

            if not fresh_snapshot:
                await self.repo.append_system_event(
                    'WARNING', 'SECOND_FILTER', 'empty token snapshot; retry later',
                    _json_dumps({'token': token_mint, 'discovery_event_id': discovery_event_id}),
                    account_type='SIM',
                )
                continue

            # fetch_token_snapshot 有时只返回价格字段；二筛前“再次调取上述特征”必须带上池子元数据。
            fresh_snapshot.setdefault('token_mint', token_mint)
            fresh_snapshot.setdefault('pool_address', event.get('pool_address') or token_row.get('pool_address') or '')
            fresh_snapshot.setdefault('pool_created_at', event.get('pool_created_at') or token_row.get('pool_created_at'))
            fresh_snapshot.setdefault('launchpad', token_row.get('launchpad'))
            if token_row.get('latest_type') and not fresh_snapshot.get('type'):
                fresh_snapshot['type'] = token_row.get('latest_type')

            source_snapshot_id = event.get('source_snapshot_id')
            recheck_snapshot_id = await self.repo.insert_token_metric_snapshot(
                token_mint,
                now.isoformat(),
                _json_dumps(fresh_snapshot),
                **_snapshot_kwargs(fresh_snapshot),
            )
            snapshot_id_for_latest = recheck_snapshot_id if recheck_snapshot_id is not None else source_snapshot_id

            if snapshot_id_for_latest is not None:
                await self.repo.update_token_latest_snapshot(
                    token_mint,
                    latest_snapshot_id=snapshot_id_for_latest,
                    latest_price_usd=_first_present(fresh_snapshot, ['price_usd'], latest.get('price_usd')),
                    latest_price_sol=_first_present(fresh_snapshot, ['price_sol'], latest.get('price_sol')),
                    latest_liquidity_usd=fresh_snapshot.get('liquidity_usd'),
                    latest_sol_side_liquidity=_first_present(fresh_snapshot, ['sol_side_liquidity'], latest.get('sol_side_liquidity')),
                    latest_market_cap=fresh_snapshot.get('market_cap'),
                    latest_type=fresh_snapshot.get('type'),
                )

            candidate_strategy_groups = await self._initial_passed_strategy_groups(discovery_event_id)
            if not candidate_strategy_groups:
                await self.repo.update_discovery_event_status(discovery_event_id, 'SECOND_SKIPPED_NO_ACTIVE_STRATEGY')
                continue

            # “一分钟后再次调取上述特征，如果依旧满足上述条件”：
            # 这里复核的是核心风控/持仓结构/平台/类型条件，不再复核 [t, t+60] 池龄窗口，
            # 否则任何在初筛窗口后半段发现的池子，等待 60s 后都会天然超窗。
            recheck_passed_groups: List[Dict[str, Any]] = []
            for sg in candidate_strategy_groups:
                try:
                    res = await run_risk_filter(fresh_snapshot, sg, now)
                    await self.repo.insert_strategy_match(
                        token_mint,
                        sg.get('id', 0),
                        sg.get('config_version', 1),
                        recheck_snapshot_id,
                        'second_core_recheck',
                        res.passed,
                        _json_dumps([d.__dict__ for d in res.details]),
                        _json_dumps(res.feature_vector),
                        discovery_event_id=discovery_event_id,
                    )
                    if res.passed:
                        recheck_passed_groups.append(sg)
                except Exception as e:
                    logger.error(f"Second initial recheck exception for {token_mint} strategy {sg.get('id')}: {e}")
                    await self.repo.append_system_event(
                        'ERROR', 'SECOND_FILTER', 'second initial recheck exception',
                        _json_dumps({'token': token_mint, 'strategy_id': sg.get('id'), 'discovery_event_id': discovery_event_id, 'error': str(e)}),
                        account_type='SIM',
                    )

            if not recheck_passed_groups:
                await self.repo.update_discovery_event_status(discovery_event_id, 'SECOND_RECHECK_FAILED')
                continue

            sorted_klines = _sort_klines(klines)
            buy_sell_1m = _extract_buy_sell_1m(sorted_klines, fresh_snapshot)
            passed_strategies: List[Dict[str, Any]] = []

            for sg in recheck_passed_groups:
                try:
                    res = await run_second_filter(fresh_snapshot, sg, latest, sorted_klines, buy_sell_1m)
                    await self.repo.insert_strategy_match(
                        token_mint,
                        sg.get('id', 0),
                        sg.get('config_version', 1),
                        recheck_snapshot_id,
                        'second_filter',
                        res.passed,
                        _json_dumps(res.details),
                        _json_dumps(res.feature_vector),
                        discovery_event_id=discovery_event_id,
                    )
                    if res.passed:
                        passed_strategies.append(sg)
                except Exception as e:
                    logger.error(f"Second filter exception for {token_mint} strategy {sg.get('id')}: {e}")
                    await self.repo.append_system_event(
                        'ERROR', 'SECOND_FILTER', 'second filter exception',
                        _json_dumps({'token': token_mint, 'strategy_id': sg.get('id'), 'discovery_event_id': discovery_event_id, 'error': str(e)}),
                        account_type='SIM',
                    )

            if passed_strategies:
                # --- Top1 holder check (addr_type=0) before entry ---
                try:
                    holders = await self.gmgn.fetch_top_holders(token_mint, limit=20)
                    top1_holders_passed: List[Dict[str, Any]] = []
                    for sg in passed_strategies:
                        x_val = float(sg.get("x", 0.2))
                        top1_threshold = 0.048 + 0.01 * x_val
                        top1_ok = True
                        top1_rate = None
                        for h in holders:
                            if int(h.get("addr_type", 0)) == 0:
                                top1_rate = _to_float(h.get("top1_holder_rate") or h.get("rate") or h.get("amount_percentage"))
                                top1_ok = top1_rate is None or top1_rate < top1_threshold
                                break
                        await self.repo.insert_strategy_match(
                            token_mint,
                            sg.get('id', 0),
                            sg.get('config_version', 1),
                            recheck_snapshot_id,
                            'top1_holder',
                            top1_ok,
                            _json_dumps({"top1_rate": top1_rate, "threshold": top1_threshold, "x": x_val}),
                            _json_dumps({"top1_rate": top1_rate, "threshold": top1_threshold}),
                            discovery_event_id=discovery_event_id,
                        )
                        if top1_ok:
                            top1_holders_passed.append(sg)
                        else:
                            logger.info(f"top1_holder fail for {token_mint} sg={sg.get('id')}: " f"rate={top1_rate} threshold={top1_threshold}")
                    passed_strategies = top1_holders_passed
                except Exception as e:
                    logger.error(f"top1 holder check failed for {token_mint}: {e}")
                    await self.repo.append_system_event(
                        'ERROR', 'SECOND_FILTER', 'top1 holder check exception',
                        _json_dumps({'token': token_mint, 'discovery_event_id': discovery_event_id, 'error': str(e)}),
                        account_type='SIM',
                    )
                    # Gracefully allow entry when top1 check fails (unblocked path)
                    pass

                if passed_strategies:
                    await self.repo.update_discovery_event_status(discovery_event_id, 'SECOND_PASSED')
                    await self.pipeline.handle_token_second_filter_result(
                        token_mint,
                        passed_strategies,
                        snapshot_id=None,
                        discovery_event_id=discovery_event_id,
                    )
                else:
                    await self.repo.update_discovery_event_status(discovery_event_id, 'TOP1_HOLDER_FAILED')
            else:
                await self.repo.update_discovery_event_status(discovery_event_id, 'SECOND_FAILED')
