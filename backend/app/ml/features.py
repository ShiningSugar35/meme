from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

import numpy as np
import pandas as pd

from ..collector.constants import LAUNCHPADS
from ..collector.filters import launchpad_key
from .types import PreparedDataset


_EXACT_EXCLUSIONS = {
    "address",
    "name",
    "symbol",
    "type",
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
}

_FUTURE_PATTERNS = (
    re.compile(r"(^|[/_])price_?2h_(max|min|close)"),
    re.compile(r"(^|[/_])final_?2h"),
    re.compile(r"(^|[/_])future([/_]|$)"),
    re.compile(r"(^|[/_])exit_(time|price|reason)"),
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
    "final_2h_close_ratio",
    "close_2h_ratio",
    "price_2h_close/price",
    "final_close_ratio",
)

_RETURN_ESTIMATE_COLUMNS = (
    "return_is_estimated",
    "terminal_return_estimated",
)

LAUNCHPAD_MODEL_FEATURES: tuple[str, ...] = tuple(
    f"launchpad::{platform}" for platform in LAUNCHPADS
)


def launchpad_feature_values(value: object) -> dict[str, int]:
    selected = launchpad_key(value)
    return {
        f"launchpad::{platform}": int(selected == launchpad_key(platform))
        for platform in LAUNCHPADS
    }


# Frozen model-input schema from README.md §2.2. `tag` is deliberately absent:
# it is the target, never an input feature. `price` and launchpad identity are
# admission-time facts. Raw liquidity remains economic sizing/evaluation data;
# only its optional log transform may be selected as a model input.
AVAILABLE_MODEL_FEATURES: tuple[str, ...] = (
    "age",
    "price",
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
    "ln(creator_open_count+1)",
    "creator_open_ratio",
    "ln(top_wallets+1)",
    *LAUNCHPAD_MODEL_FEATURES,
)

# `ln(liquidity_usd)` is collected for new samples but intentionally excluded
# from the default recipe because the legacy CSV cannot reconstruct raw entry
# liquidity. Operators may opt it in later once enough new samples have it.
DEFAULT_MODEL_TRAINING_FEATURES: tuple[str, ...] = tuple(
    feature for feature in AVAILABLE_MODEL_FEATURES if feature != "ln(liquidity_usd)"
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
        # Entry price is normally excluded by the generic leakage policy for
        # backwards compatibility. A caller using an explicit allowlist may opt
        # it in because README defines it as an admission-time feature.
        if normalized == "price" and self.feature_allowlist is not None:
            return normalized not in {item.strip().lower() for item in self.feature_allowlist}
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

        if "launchpad" in work.columns:
            launchpad_keys = work["launchpad"].map(launchpad_key)
            for platform, feature_name in zip(LAUNCHPADS, LAUNCHPAD_MODEL_FEATURES, strict=True):
                work[feature_name] = (launchpad_keys == launchpad_key(platform)).astype(int)

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
            feature_names=tuple(feature_columns),
            dropped_columns=tuple(dict.fromkeys(excluded)),
            source_rows=pd.Index(work["_source_row"].to_numpy()),
        )
