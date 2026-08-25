from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from .types import PreparedDataset


_EXACT_EXCLUSIONS = {
    "address",
    "name",
    "symbol",
    "type",
    "launchpad",
    "time",
    "timestamp",
    "created_at",
    "price",
    "tag",
    "target",
    "label",
    "dexscr_update_link",
    "return_is_estimated",
    "terminal_return_estimated",
    "utility_eligible",
    "execution_invested_usd",
    "execution_net_pnl_usd",
    "execution_observed",
}

_FUTURE_PATTERNS = (
    re.compile(r"(^|[/_])price_?1h_(max|min|close)"),
    re.compile(r"(^|[/_])price_?2h_(max|min|close)"),
    re.compile(r"(^|[/_])final_?1h"),
    re.compile(r"(^|[/_])final_?2h"),
    re.compile(r"(^|[/_])future([/_]|$)"),
    re.compile(r"(^|[/_])exit_(time|price|reason)"),
    re.compile(r"(^|[/_])label_(max_price_ratio|min_price_ratio|final_close_ratio|window_seconds)"),
    re.compile(r"(^|[/_])first_(take_profit|stop_loss)_at"),
    re.compile(r"(^|[/_])(gross|net)_return"),
    re.compile(r"(^|[/_])same_bar_conflict"),
    re.compile(r"^tag([/_]|$)"),
    re.compile(r"(^|[_/])(token|pool|contract|mint|wallet)?_?address$"),
    re.compile(r"(^|[_/])(transaction|tx)_?(id|hash|signature)$"),
)

_LIQUIDITY_COLUMNS = (
    "liquidity",
    "liquidity_usd",
    "pool_liquidity",
    "pool_liquidity_usd",
)

_FINAL_CLOSE_COLUMNS = (
    "final_1h_close_ratio",
    "final_2h_close_ratio",
    "close_2h_ratio",
    "price_2h_close/price",
    "final_close_ratio",
)

_RETURN_ESTIMATE_COLUMNS = (
    "return_is_estimated",
    "terminal_return_estimated",
)

AGE_LOG1P_FEATURE = "ln(age+1)"
PRICE_LOG1P_FEATURE = "ln(price+1)"
LEGACY_AGE_FEATURE = "age"
LEGACY_PRICE_FEATURE = "price"


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def materialize_entry_feature(
    name: str,
    source: Mapping[str, Any],
    *,
    entry_price: Any = None,
) -> Any:
    """Resolve current feature semantics while keeping old model schemas scoreable.

    Historical samples/models use ``age=ln(age_minutes)`` and raw ``price``.
    New models use log1p for both features. This adapter lets current training
    reconstruct the new age feature from legacy samples and lets retired/active
    legacy models continue scoring samples collected after the schema change.
    """

    if name == PRICE_LOG1P_FEATURE:
        price = _finite_float(entry_price)
        return math.log1p(price) if price is not None and price >= 0 else None
    if name == LEGACY_PRICE_FEATURE:
        return _finite_float(entry_price)

    if name == AGE_LOG1P_FEATURE:
        current = _finite_float(source.get(AGE_LOG1P_FEATURE))
        if current is not None:
            return current
        legacy = _finite_float(source.get(LEGACY_AGE_FEATURE))
        if legacy is None:
            return None
        try:
            return math.log1p(math.exp(legacy))
        except OverflowError:
            return None

    if name == LEGACY_AGE_FEATURE:
        legacy = _finite_float(source.get(LEGACY_AGE_FEATURE))
        if legacy is not None:
            return legacy
        current = _finite_float(source.get(AGE_LOG1P_FEATURE))
        if current is None:
            return None
        try:
            age_minutes = math.expm1(current)
        except OverflowError:
            return None
        return math.log(age_minutes) if age_minutes > 0 else None

    return source.get(name)


# Frozen model-input schema from README.md §2.2. `tag` is deliberately absent:
# it is the target, never an input feature. Price and age are admission-time
# facts whose model-facing representations use log1p transforms.
# Launchpad remains sample metadata and is deliberately excluded from model
# inputs. Raw liquidity remains economic sizing/evaluation data; only its
# optional log transform may be selected as a model input.
AVAILABLE_MODEL_FEATURES: tuple[str, ...] = (
    AGE_LOG1P_FEATURE,
    PRICE_LOG1P_FEATURE,
    "ln(liquidity_usd)",
    "liquidity/holder_count",
    "volume_1h/swaps_1h",
    "has_twitter",
    "has_website",
    "ln(image_dup+1)",
    "dexscr_update_link",
    "cto_flag",
    "ln(twitter_rename_count+1)",
    "ln(twitter_del_post_token_count+1)",
    "ln(twitter_create_token_count+1)",
    "top_10_holder_rate",
    "top_bot_degen_percentage",
    "fresh_wallet_rate",
    "bot_degen_rate",
    "price/ath_price",
    "stat.holder_count/market_cap",
    "ln(smart_degen_count+1)",
    "ln(renowned_count+1)",
    "entrapment_ratio",
    "dev_team_hold_rate",
    "top70_sniper_hold_rate",
    "ln(twitter_dup+1)",
    "ln(website_dup+1)",
    "ln(visiting_count+1)",
    "price_change_1h",
    "price_change_5m",
    "price_change_1m",
    "ln(volume_1m+1)",
    "buy_count_imbalance_1m",
    "buy_volume_imbalance_1m",
    "ln(volume_1m/swaps_1m+1)",
    "holder_count/age",
    "ln(marketcap+1)",
    "dexscr_ad",
    "ln(dexscr_boost_fee+1)",
    "dexscr_trending_bar",
    "ln(x_user_follower+1)",
    "ln(tg_call_count+1)",
    "ln(creator_open_count+1)",
    "creator_open_ratio",
    "ln(top_wallets+1)",
)

# Event1m samples are generation-isolated from legacy CSV/pre-event rows;
# event features participate in chronological OOS competition rather than being auto-promoted.
# The trainer's feature-state key is also namespaced by feature generation.
DEFAULT_MODEL_TRAINING_FEATURES: tuple[str, ...] = (
    AGE_LOG1P_FEATURE,
    PRICE_LOG1P_FEATURE,
    "liquidity/holder_count",
    "volume_1h/swaps_1h",
    "has_twitter",
    "has_website",
    "ln(image_dup+1)",
    "dexscr_update_link",
    "cto_flag",
    "ln(twitter_rename_count+1)",
    "ln(twitter_del_post_token_count+1)",
    "ln(twitter_create_token_count+1)",
    "top_10_holder_rate",
    "top_bot_degen_percentage",
    "fresh_wallet_rate",
    "bot_degen_rate",
    "price/ath_price",
    "stat.holder_count/market_cap",
    "ln(smart_degen_count+1)",
    "ln(renowned_count+1)",
    "entrapment_ratio",
    "dev_team_hold_rate",
    "top70_sniper_hold_rate",
    "ln(twitter_dup+1)",
    "ln(website_dup+1)",
    "ln(visiting_count+1)",
    "price_change_1h",
    "price_change_5m",
    "ln(creator_open_count+1)",
    "creator_open_ratio",
    "ln(top_wallets+1)",
)
# Backward-compatible name used by existing services/tests: this always means
# the default production feature set, not the full selectable catalog.
MODEL_TRAINING_FEATURES = DEFAULT_MODEL_TRAINING_FEATURES


@dataclass(frozen=True)
class FeaturePolicy:
    target_column: str = "tag"
    time_column: str = "time"
    extra_exclusions: tuple[str, ...] = ()
    feature_allowlist: tuple[str, ...] | None = None

    def is_excluded(self, column: str) -> bool:
        normalized = column.strip().lower()
        if normalized in _EXACT_EXCLUSIONS:
            return True
        if normalized in {item.strip().lower() for item in self.extra_exclusions}:
            return True
        return any(pattern.search(normalized) for pattern in _FUTURE_PATTERNS)


def _first_existing(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    lookup = {column.strip().lower(): column for column in columns}
    for candidate in candidates:
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    return None


def parse_event_time(values: pd.Series) -> pd.Series:
    """Parse seconds, milliseconds, or ISO strings into UTC timestamps."""

    numeric = pd.to_numeric(values, errors="coerce")
    result = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns, UTC]")
    numeric_mask = numeric.notna()
    if numeric_mask.any():
        milliseconds = numeric_mask & (numeric.abs() >= 1e11)
        seconds = numeric_mask & ~milliseconds
        if milliseconds.any():
            result.loc[milliseconds] = pd.to_datetime(
                numeric.loc[milliseconds], unit="ms", utc=True, errors="coerce"
            )
        if seconds.any():
            result.loc[seconds] = pd.to_datetime(
                numeric.loc[seconds], unit="s", utc=True, errors="coerce"
            )

    text_mask = ~numeric_mask
    if text_mask.any():
        result.loc[text_mask] = pd.to_datetime(
            values.loc[text_mask], utc=True, errors="coerce"
        )
    return result


class FeatureBuilder:
    """Create a leak-free, chronologically sorted binary classification set."""

    def __init__(self, policy: FeaturePolicy | None = None) -> None:
        self.policy = policy or FeaturePolicy()

    def prepare(self, frame: pd.DataFrame) -> PreparedDataset:
        if frame.empty:
            raise ValueError("training data is empty")
        if self.policy.target_column not in frame.columns:
            raise ValueError(f"missing target column: {self.policy.target_column}")
        if self.policy.time_column not in frame.columns:
            raise ValueError(f"missing event time column: {self.policy.time_column}")

        work = frame.copy()
        target = pd.to_numeric(work[self.policy.target_column], errors="coerce")
        timestamps = parse_event_time(work[self.policy.time_column])
        complete = target.notna() & timestamps.notna()
        work = work.loc[complete].copy()
        target = target.loc[complete].astype(int)
        timestamps = timestamps.loc[complete]

        invalid_tags = sorted(set(target.unique()) - {0, 1})
        if invalid_tags:
            raise ValueError(f"unsupported binary tag values: {invalid_tags}")
        if work.empty:
            raise ValueError("no rows have both a mature tag and valid event time")

        order = np.argsort(timestamps.to_numpy(), kind="stable")
        work = work.iloc[order].reset_index(drop=False).rename(columns={"index": "_source_row"})
        target = target.iloc[order].reset_index(drop=True)
        timestamps = timestamps.iloc[order].reset_index(drop=True)

        liquidity_column = _first_existing(work.columns, _LIQUIDITY_COLUMNS)
        if liquidity_column is None:
            liquidity = pd.Series(np.nan, index=work.index, dtype=float)
        else:
            liquidity = pd.to_numeric(work[liquidity_column], errors="coerce").astype(float)
            liquidity = liquidity.where(liquidity >= 0)

        close_column = _first_existing(work.columns, _FINAL_CLOSE_COLUMNS)
        if close_column is None:
            close_ratio = pd.Series(np.nan, index=work.index, dtype=float)
        else:
            close_ratio = pd.to_numeric(work[close_column], errors="coerce").astype(float)

        # Binary labels no longer use a timeout-positive class. Final close is
        # retained only as an audit fact and never changes the target/economics.
        return_is_estimated = pd.Series(False, index=work.index, dtype=bool)
        execution_invested = pd.to_numeric(
            work.get("execution_invested_usd", pd.Series(np.nan, index=work.index)),
            errors="coerce",
        ).astype(float)
        execution_net_pnl = pd.to_numeric(
            work.get("execution_net_pnl_usd", pd.Series(np.nan, index=work.index)),
            errors="coerce",
        ).astype(float)
        observed_raw = work.get("execution_observed", pd.Series(False, index=work.index))
        execution_observed = observed_raw.fillna(False).astype(bool)
        execution_observed &= execution_invested.gt(0) & execution_net_pnl.notna()

        if self.policy.feature_allowlist is not None:
            # A frozen allowlist prevents newly added DB/audit columns from
            # silently becoming model inputs. Preserve the declared order and
            # materialize temporarily missing fields as NaN so train/inference
            # schemas stay stable across collection vintages.
            for column in self.policy.feature_allowlist:
                if column not in work.columns:
                    work[column] = np.nan
                work[column] = pd.to_numeric(work[column], errors="coerce")
            feature_columns = list(self.policy.feature_allowlist)
            excluded = [
                column
                for column in work.columns
                if column not in set(feature_columns)
            ]
        else:
            excluded = [column for column in work.columns if self.policy.is_excluded(column)]
            excluded.append("_source_row")
            feature_columns = [column for column in work.columns if column not in set(excluded)]

            # Generic callers retain the old behavior of dropping truly empty
            # columns. Production training uses the frozen allowlist above.
            feature_columns = [column for column in feature_columns if work[column].notna().any()]
        if not feature_columns:
            raise ValueError("no usable entry-time features remain after leakage exclusions")

        X = work.loc[:, feature_columns].copy()
        y = target.eq(1).astype(int)
        return PreparedDataset(
            X=X,
            y=y,
            tags=target,
            timestamps=timestamps,
            liquidity_usd=liquidity.reset_index(drop=True),
            final_close_ratio=close_ratio.reset_index(drop=True),
            return_is_estimated=return_is_estimated.reset_index(drop=True),
            execution_invested_usd=execution_invested.reset_index(drop=True),
            execution_net_pnl_usd=execution_net_pnl.reset_index(drop=True),
            execution_observed=execution_observed.reset_index(drop=True),
            feature_names=tuple(feature_columns),
            dropped_columns=tuple(dict.fromkeys(excluded)),
            source_rows=pd.Index(work["_source_row"].to_numpy()),
        )
