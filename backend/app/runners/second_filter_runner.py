import json
from datetime import datetime, timedelta, timezone
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
    'has_at_least_one_social',
    'creator_token_status',
    'burn_status',
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



def _interval_seconds(interval: str) -> int:
    value = str(interval or '1m').strip().lower()
    if value.endswith('m'):
        return max(1, int(float(value[:-1]) * 60))
    if value.endswith('h'):
        return max(1, int(float(value[:-1]) * 3600))
    if value.endswith('d'):
        return max(1, int(float(value[:-1]) * 86400))
    return 60


def _completed_klines(klines: List[Dict[str, Any]], interval: str, now: datetime) -> List[Dict[str, Any]]:
    span = timedelta(seconds=_interval_seconds(interval))
    completed: List[Dict[str, Any]] = []
    for item in _sort_klines(klines):
        opened = _parse_dt(_first_present(item, ['open_time', 'time', 'timestamp', 't']))
        if not opened:
            continue
        if opened + span <= now:
            completed.append(item)
    return completed


def _median(values: List[float]) -> Optional[float]:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def _kline_precheck(snapshot: Dict[str, Any], latest: Dict[str, Any], klines_1m: List[Dict[str, Any]], klines_5m: List[Dict[str, Any]], sg: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    """Cheap local gate before calling the top-holder endpoint.

    Mirrors the strategy's K-line predicates closely enough to keep top-holder
    API calls as the last expensive check. run_second_filter remains the final
    authority and writes the canonical decision detail.
    """
    y = _to_float(sg.get('y')) or 2.25
    latest_1m = klines_1m[-1] if klines_1m else {}
    if not latest_1m:
        return False, {'reason': 'no_completed_1m_candle'}

    open_1m = _to_float(_first_present(latest_1m, ['open', 'o']))
    high_1m = _to_float(_first_present(latest_1m, ['high', 'h']))
    low_1m = _to_float(_first_present(latest_1m, ['low', 'l']))
    close_1m = _to_float(_first_present(latest_1m, ['close', 'c']))
    volume_1m = _to_float(_first_present(latest_1m, ['volume_usd', 'volume', 'v'])) or 0.0
    liquidity = _to_float(snapshot.get('liquidity_usd')) or _to_float(latest.get('liquidity_usd')) or 0.0

    if open_1m is None or high_1m is None or low_1m is None or close_1m is None:
        return False, {'reason': 'incomplete_1m_candle'}
    range_1m = high_1m - low_1m
    if range_1m <= 0:
        return False, {'reason': 'zero_1m_range'}

    prev_volumes = [
        _to_float(_first_present(k, ['volume_usd', 'volume', 'v'])) or 0.0
        for k in klines_1m[-6:-1]
    ]
    median_prev = _median(prev_volumes) or 0.0
    volume_threshold = max(liquidity * max(0.0, (0.07 - 0.02 * y)), median_prev * max(0.0, (1.3 - 0.1 * y)))
    close_threshold = open_1m * (1 - 0.002 * y)
    close_position_1m = (close_1m - low_1m) / range_1m

    range_source = (klines_5m[-1:] if klines_5m else []) or klines_1m[-5:]
    highs = [_to_float(_first_present(k, ['high', 'h'])) for k in range_source]
    lows = [_to_float(_first_present(k, ['low', 'l'])) for k in range_source]
    highs = [v for v in highs if v is not None]
    lows = [v for v in lows if v is not None]
    if not highs or not lows:
        return False, {'reason': 'no_5m_range'}
    high_5m = max(highs)
    low_5m = min(lows)
    range_5m = high_5m - low_5m
    if range_5m <= 0:
        return False, {'reason': 'zero_5m_range'}

    current_price = _to_float(_first_present(latest, ['price_usd', 'price', 'latest_price_usd'])) or close_1m
    pos_5m = (current_price - low_5m) / range_5m

    checks = {
        'volume_1m': volume_1m > volume_threshold,
        'close_1m': close_1m > close_threshold,
        'close_position_1m': close_position_1m > (0.80 - 0.01 * y),
        'price_gt_high_5m_over_y': current_price > high_5m / y,
        'price_lt_low_5m_times_y': current_price < low_5m * y,
        'price_position_5m': (0.8 - 0.2 * y) < pos_5m < (0.35 + 0.2 * y),
    }
    features = {
        'y': y,
        'open_1m': open_1m,
        'high_1m': high_1m,
        'low_1m': low_1m,
        'close_1m': close_1m,
        'volume_1m': volume_1m,
        'volume_threshold': volume_threshold,
        'median_volume_prev_5m': median_prev,
        'close_position_1m': close_position_1m,
        'current_price': current_price,
        'high_5m': high_5m,
        'low_5m': low_5m,
        'price_position_5m': pos_5m,
        'checks': checks,
    }
    return all(checks.values()), features


def _top1_threshold(sg: Dict[str, Any]) -> float:
    x = _to_float(sg.get('x'))
    if x is None:
        x = 0.2
    return 0.048 + 0.01 * x


async def _fetch_top1_holder_rate(provider: MarketDataProvider, token_mint: str) -> Optional[float]:
    if not hasattr(provider, 'fetch_top1_holder_rate'):
        return None
    data = await provider.fetch_top1_holder_rate(token_mint, addr_type=0)  # type: ignore[attr-defined]
    return _to_float((data or {}).get('top1_holder_rate') if isinstance(data, dict) else None)


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
        self.strategy_groups = strategy_groups
        self.pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)

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
        return groups or self.strategy_groups

    async def run_once(self):
        now = datetime.now(timezone.utc)
        mode = settings.get_provider_mode()

        # 只处理初筛已经通过、但还没有进入二筛终态的池子。
        discovery_events = await self.repo.list_discovery_events(status='INITIAL_PASSED', limit=200)

        for event in discovery_events:
            token_mint = event['token_mint']
            discovery_event_id = event['id']

            if mode != ProviderMode.MOCK and token_mint in MOCK_MINTS:
                continue

            if mode != ProviderMode.MOCK:
                first_seen = _parse_dt(event.get('first_seen_at'))
                if first_seen and (now - first_seen).total_seconds() < 60:
                    continue

            token_row = await self.repo.get_token(token_mint) or {}
            try:
                fresh_snapshot = await self.gmgn.fetch_token_snapshot(token_mint)
                latest = await self.gmgn.fetch_latest_price(token_mint)
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

            recheck_snapshot_id = fresh_snapshot.get('snapshot_id')
            source_snapshot_id = event.get('source_snapshot_id')
            snapshot_id_for_latest = recheck_snapshot_id if recheck_snapshot_id is not None else source_snapshot_id

            await self.repo.insert_token_metric_snapshot(
                token_mint,
                now.isoformat(),
                _json_dumps(fresh_snapshot),
                **_snapshot_kwargs(fresh_snapshot),
            )

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

            try:
                raw_klines_1m = await self.gmgn.fetch_kline(token_mint, '1m', 8)
                raw_klines_5m = await self.gmgn.fetch_kline(token_mint, '5m', 2)
            except Exception as e:
                await self.repo.append_system_event(
                    'ERROR', 'SECOND_FILTER', 'GMGN kline fetch failed',
                    _json_dumps({'token': token_mint, 'discovery_event_id': discovery_event_id, 'error': str(e)}),
                    account_type='SIM',
                )
                continue

            sorted_klines = _completed_klines(raw_klines_1m, '1m', now)
            sorted_klines_5m = _completed_klines(raw_klines_5m, '5m', now)
            if not sorted_klines:
                await self.repo.update_discovery_event_status(discovery_event_id, 'SECOND_FAILED', fail_reason_json=_json_dumps({'reason': 'no_completed_1m_candle'}))
                continue

            precheck_passed_groups: List[Dict[str, Any]] = []
            precheck_features: Dict[int, Dict[str, Any]] = {}
            for sg in recheck_passed_groups:
                ok, features = _kline_precheck(fresh_snapshot, latest, sorted_klines, sorted_klines_5m, sg)
                precheck_features[int(sg.get('id', 0))] = features
                if ok:
                    precheck_passed_groups.append(sg)
                else:
                    await self.repo.insert_strategy_match(
                        token_mint,
                        sg.get('id', 0),
                        sg.get('config_version', 1),
                        recheck_snapshot_id,
                        'second_kline_precheck',
                        False,
                        _json_dumps(features),
                        _json_dumps(features),
                        discovery_event_id=discovery_event_id,
                    )

            if not precheck_passed_groups:
                await self.repo.update_discovery_event_status(discovery_event_id, 'SECOND_FAILED', fail_reason_json=_json_dumps({'reason': 'kline_precheck_failed'}))
                continue

            # Top holders are intentionally deferred until after snapshot + K-line checks pass.
            top1_rate = await _fetch_top1_holder_rate(self.gmgn, token_mint)
            fresh_snapshot['top1_holder_rate'] = top1_rate
            for sg in precheck_passed_groups:
                threshold = _top1_threshold(sg)
                if top1_rate is None or top1_rate >= threshold:
                    await self.repo.insert_strategy_match(
                        token_mint,
                        sg.get('id', 0),
                        sg.get('config_version', 1),
                        recheck_snapshot_id,
                        'second_top1_holder_check',
                        False,
                        _json_dumps({'top1_holder_rate': top1_rate, 'threshold': threshold}),
                        _json_dumps({'top1_holder_rate': top1_rate, 'threshold': threshold}),
                        discovery_event_id=discovery_event_id,
                    )
            holder_passed_groups = [sg for sg in precheck_passed_groups if top1_rate is not None and top1_rate < _top1_threshold(sg)]
            if not holder_passed_groups:
                await self.repo.update_discovery_event_status(discovery_event_id, 'SECOND_FAILED', fail_reason_json=_json_dumps({'reason': 'top1_holder_failed', 'top1_holder_rate': top1_rate}))
                continue

            fresh_snapshot['completed_1m_candles'] = sorted_klines
            fresh_snapshot['completed_5m_candles'] = sorted_klines_5m
            # Give downstream strategy code direct access to the derived 5m range.
            sample_for_5m = (sorted_klines_5m[-1:] if sorted_klines_5m else []) or sorted_klines[-5:]
            fresh_snapshot['high_5m'] = max((_to_float(_first_present(k, ['high', 'h'])) or 0.0) for k in sample_for_5m)
            fresh_snapshot['low_5m'] = min((_to_float(_first_present(k, ['low', 'l'])) or 0.0) for k in sample_for_5m)

            buy_sell_1m = _extract_buy_sell_1m(sorted_klines, fresh_snapshot)
            passed_strategies: List[Dict[str, Any]] = []

            for sg in holder_passed_groups:
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
                await self.repo.update_discovery_event_status(discovery_event_id, 'SECOND_PASSED')
                # 注意：这里故意不传 source_snapshot_id；executor 当前会先用 snapshot_id 做重复 discovery 检查，
                # 传入初筛 snapshot_id 会把自己判成 duplicate，从而不买入/不建模拟仓。
                await self.pipeline.handle_token_second_filter_result(
                    token_mint,
                    passed_strategies,
                    snapshot_id=None,
                    discovery_event_id=discovery_event_id,
                )
            else:
                await self.repo.update_discovery_event_status(discovery_event_id, 'SECOND_FAILED')
