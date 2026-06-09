from __future__ import annotations

import os
import random
import re
import warnings
from enum import Enum
from typing import Dict, List, Optional

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .logging_config import logger

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class ProviderMode(str, Enum):
    MOCK = "mock"
    ONLINE_READONLY = "online_readonly"
    LIVE = "live"


def _secret_to_str(value: Optional[SecretStr | str]) -> Optional[str]:
    if value is None:
        return None
    raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    raw = (raw or "").strip()
    return raw or None


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [x.strip() for x in str(value).split(",") if x and x.strip()]


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_ENV: str = Field("development")
    SQLITE_PATH: str = Field("./data/trading_bot.sqlite3")

    PROVIDER_MODE: Optional[ProviderMode] = Field(None)
    DRY_RUN: bool = Field(True)
    LIVE_TRADING_ENABLED: Optional[bool] = None
    PRIVATE_KEY: Optional[SecretStr] = None
    WALLET_ADDRESS: Optional[str] = None
    SOLANA_RPC_URL: Optional[str] = None
    JUPITER_API_BASE: Optional[str] = None
    SIMULATION_ENABLED: bool = Field(True)

    GMGN_API_BASE_URL: Optional[str] = None
    GMGN_API_KEY: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY: Optional[str] = None
    GMGN_PRIVATE_KEY: Optional[SecretStr] = None
    GMGN_CLIENT_ID: Optional[str] = None
    GMGN_API_KEY_1: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_1: Optional[str] = None
    GMGN_PRIVATE_KEY_1: Optional[SecretStr] = None
    GMGN_API_KEY_2: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_2: Optional[str] = None
    GMGN_PRIVATE_KEY_2: Optional[SecretStr] = None
    GMGN_API_KEY_3: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_3: Optional[str] = None
    GMGN_PRIVATE_KEY_3: Optional[SecretStr] = None
    GMGN_API_KEY_4: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_4: Optional[str] = None
    GMGN_PRIVATE_KEY_4: Optional[SecretStr] = None
    GMGN_API_KEY_5: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_5: Optional[str] = None
    GMGN_PRIVATE_KEY_5: Optional[SecretStr] = None
    GMGN_API_KEY_6: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_6: Optional[str] = None
    GMGN_PRIVATE_KEY_6: Optional[SecretStr] = None
    GMGN_API_KEY_7: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_7: Optional[str] = None
    GMGN_PRIVATE_KEY_7: Optional[SecretStr] = None
    GMGN_API_KEY_8: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_8: Optional[str] = None
    GMGN_PRIVATE_KEY_8: Optional[SecretStr] = None
    GMGN_API_KEY_9: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_9: Optional[str] = None
    GMGN_PRIVATE_KEY_9: Optional[SecretStr] = None
    GMGN_API_KEY_10: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_10: Optional[str] = None
    GMGN_PRIVATE_KEY_10: Optional[SecretStr] = None
    GMGN_API_KEY_11: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_11: Optional[str] = None
    GMGN_PRIVATE_KEY_11: Optional[SecretStr] = None
    GMGN_API_KEY_12: Optional[SecretStr] = None
    GMGN_PUBLIC_KEY_12: Optional[str] = None
    GMGN_PRIVATE_KEY_12: Optional[SecretStr] = None
    GMGN_CLIENT_ID_1: Optional[str] = None
    GMGN_CLIENT_ID_2: Optional[str] = None
    GMGN_CLIENT_ID_3: Optional[str] = None
    GMGN_CLIENT_ID_4: Optional[str] = None
    GMGN_CLIENT_ID_5: Optional[str] = None
    GMGN_CLIENT_ID_6: Optional[str] = None
    GMGN_CLIENT_ID_7: Optional[str] = None
    GMGN_CLIENT_ID_8: Optional[str] = None
    GMGN_CLIENT_ID_9: Optional[str] = None
    GMGN_CLIENT_ID_10: Optional[str] = None
    GMGN_CLIENT_ID_11: Optional[str] = None
    GMGN_CLIENT_ID_12: Optional[str] = None

    GMGN_TRENCHES_PATH: str = Field("/v1/trenches")
    GMGN_TRENCHES_METHOD: str = Field("POST")
    GMGN_TRENCHES_TYPES: str = Field("new_creation")
    GMGN_TRENCHES_PLATFORMS: str = Field("")
    GMGN_TOKEN_PRICE_PATH: str = Field("/v1/token/info")
    GMGN_KLINE_PATH: str = Field("/v1/market/token_kline")
    GMGN_TOKEN_KLINE_PATH: Optional[str] = None
    GMGN_TOKEN_INFO_PATH: str = Field("/v1/token/info")
    GMGN_TOKEN_SECURITY_PATH: str = Field("/v1/token/security")
    GMGN_TOKEN_POOL_INFO_PATH: str = Field("/v1/token/pool_info")
    GMGN_TOKEN_HOLDERS_PATH: str = Field("/v1/market/token_top_holders")
    GMGN_TIMEOUT_SECONDS: float = Field(8.0)

    GMGN_DISCOVERY_PRIMARY_SLOT: int = Field(0)
    GMGN_DISCOVERY_RESERVE_SLOT: Optional[int] = Field(None)
    GMGN_FEATURE_SLOTS: str = Field("")
    GMGN_DISCOVERY_MODE: str = Field("two_group")
    GMGN_DISCOVERY_GROUP_DELAY_SECONDS: float = Field(2.0)
    GMGN_RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS: int = Field(300)

    GMGN_RATE_LIMIT_REFILL_WEIGHT_PER_SECOND: float = Field(20.0)
    GMGN_RATE_LIMIT_BUCKET_CAPACITY: float = Field(20.0)
    GMGN_RATE_LIMIT_BUCKET_WAIT_MAX_SECONDS: float = Field(5.0)

    GMGN_POST_TRENCHES_STAGE_DELAY_SECONDS: float = Field(2.0)
    GMGN_FEATURE_CALL_DELAY_SECONDS: float = Field(0.15)
    GMGN_TRENCHES_CONCURRENCY: int = Field(2)
    GMGN_FEATURE_CONCURRENCY: int = Field(3)
    GMGN_TRENCHES_DEBUG_RELAXED_ON_ZERO: bool = Field(False)

    STRATEGY_DEFAULT_X: float = Field(0.20)
    GMGN_TRENCHES_LIMIT: int = Field(200)

    _slot_plan: Optional[dict] = None

    def _compute_slot_plan(self) -> dict:
        total = len(self.get_gmgn_credentials())
        if total < 4:
            logger.warning("gmgn_slot_plan_low_key_count total=%d, discovery/feature slots reduced", total)
        if total >= 4:
            reserve = self.GMGN_DISCOVERY_RESERVE_SLOT
            if reserve is not None and (reserve < 0 or reserve >= total):
                logger.warning("gmgn_slot_plan_reserve_out_of_bounds requested=%d total=%d, falling back to random", reserve, total)
                reserve = None
            if reserve is None:
                reserve = random.Random(str(os.getpid())).choice(range(total))
            remaining = [s for s in range(total) if s != reserve]
            discovery_slots = remaining[:2]
            return {
                "discovery_new_creation": [discovery_slots[0]] if len(discovery_slots) > 0 else [],
                "discovery_near_completion": [discovery_slots[1]] if len(discovery_slots) > 1 else [],
                "discovery_reserve": [reserve],
                "feature_holding_pool": remaining[2:],
            }
        elif total == 3:
            return {
                "discovery_new_creation": [0],
                "discovery_near_completion": [1],
                "discovery_reserve": [],
                "feature_holding_pool": [2],
            }
        elif total == 2:
            return {
                "discovery_new_creation": [0],
                "discovery_near_completion": [],
                "discovery_reserve": [],
                "feature_holding_pool": [1],
            }
        elif total == 1:
            return {
                "discovery_new_creation": [0],
                "discovery_near_completion": [],
                "discovery_reserve": [],
                "feature_holding_pool": [0],
            }
        else:
            return {
                "discovery_new_creation": [],
                "discovery_near_completion": [],
                "discovery_reserve": [],
                "feature_holding_pool": [],
            }

    def _get_slot_plan(self) -> dict:
        if self._slot_plan is None:
            self._slot_plan = self._compute_slot_plan()
            plan = self._slot_plan
            logger.info(
                "gmgn_slot_plan total_credentials=%d discovery_new_creation=%s discovery_near_completion=%s reserve=%s feature_count=%d",
                len(self.get_gmgn_credentials()),
                plan["discovery_new_creation"],
                plan["discovery_near_completion"],
                plan["discovery_reserve"],
                len(plan["feature_holding_pool"]),
            )
        return self._slot_plan

    def get_discovery_primary_slot(self) -> int:
        return self.GMGN_DISCOVERY_PRIMARY_SLOT

    def get_discovery_reserve_slot(self) -> Optional[int]:
        plan = self._get_slot_plan()
        slots = plan["discovery_reserve"]
        return slots[0] if slots else None

    def get_discovery_type_slots(self) -> Dict[str, Optional[int]]:
        plan = self._get_slot_plan()
        return {
            "new_creation": plan["discovery_new_creation"][0] if plan["discovery_new_creation"] else None,
            "near_completion": plan["discovery_near_completion"][0] if plan["discovery_near_completion"] else None,
        }

    def get_discovery_slots(self) -> List[int]:
        plan = self._get_slot_plan()
        return plan["discovery_new_creation"] + plan["discovery_near_completion"]

    def get_feature_slots(self) -> List[int]:
        return list(self._get_slot_plan()["feature_holding_pool"])

    def get_holding_slots(self) -> List[int]:
        return list(self._get_slot_plan()["feature_holding_pool"])

    def get_gmgn_slot_pools(self) -> Dict[str, List[int]]:
        plan = self._get_slot_plan()
        feature_slots = list(plan["feature_holding_pool"])
        return {
            "discovery_new_creation": list(plan["discovery_new_creation"]),
            "discovery_near_completion": list(plan["discovery_near_completion"]),
            "discovery_reserve": list(plan["discovery_reserve"]),
            "token_info": feature_slots,
            "kline": feature_slots,
            "holders": feature_slots,
            "holding": feature_slots,
            "feature": feature_slots,
        }

    JUPITER_API_BASE_URL: Optional[str] = None
    JUPITER_API_KEY_1: Optional[SecretStr] = None
    JUPITER_API_KEY_2: Optional[SecretStr] = None
    JUPITER_API_KEY_3: Optional[SecretStr] = None
    JUPITER_API_KEY_MEME1: Optional[SecretStr] = None
    JUPITER_API_KEY_MEME2: Optional[SecretStr] = None
    JUPITER_API_KEY_MEME3: Optional[SecretStr] = None

    SOLANA_RPC_HTTP_URLS: Optional[str] = None
    SOLANA_RPC_HTTP_PRIMARY: Optional[str] = None
    SOLANA_RPC_WS_PRIMARY: Optional[str] = None
    SOLANA_RPC_HTTP_BACKUP_1: Optional[str] = None
    SOLANA_RPC_WS_BACKUP_1: Optional[str] = None
    ALCHEMY_API_KEYS: Optional[str] = None
    ALCHEMY_API_KEY_1: Optional[SecretStr] = None
    ALCHEMY_API_KEY_2: Optional[SecretStr] = None
    ALCHEMY_API_KEY_3: Optional[SecretStr] = None
    ALCHEMY_API_KEY_4: Optional[SecretStr] = None
    ANKR_API_KEY_1: Optional[SecretStr] = None
    ANKR_API_KEY_2: Optional[SecretStr] = None

    JITO_ENABLED: bool = Field(True)
    JITO_BLOCK_ENGINE_URL: str = Field("https://mainnet.block-engine.jito.wtf")
    JITO_TIP_FLOOR_URL: str = Field("https://bundles.jito.wtf/api/v1/bundles/tip_floor")
    JITO_TIP_STREAM_WS: str = Field("wss://bundles.jito.wtf/api/v1/bundles/tip_stream")

    POLL_INTERVAL_SECONDS: int = Field(60)
    ACTIVE_POSITION_PRICE_POLL_SECONDS: int = Field(1)
    TIP_FLOOR_REFRESH_SECONDS: int = Field(3)
    BUY_SLIPPAGE_CAP_BPS: int = Field(1500)
    SELL_SLIPPAGE_CAP_BPS: int = Field(2000)
    EMERGENCY_SLIPPAGE_CAP_BPS: int = Field(3500)
    PRICE_IMPACT_HARD_CAP_PCT: float = Field(10.0)
    LIVE_ROLLING_10_LOSS_LIMIT: float = Field(-0.20)
    MAX_REQUOTE_RETRY: int = Field(2)
    ENTRY_SIZE_LIQUIDITY_PCT: float = Field(0.015)
    ENTRY_MAX_USD: float = Field(150.0)

    RISK_FEATURE_SCAN_TIER_1_USD: float = Field(150.0)
    RISK_FEATURE_SCAN_TIER_1_SECONDS: int = Field(4)
    RISK_FEATURE_SCAN_TIER_2_USD: float = Field(100.0)
    RISK_FEATURE_SCAN_TIER_2_SECONDS: int = Field(8)
    RISK_FEATURE_SCAN_TIER_3_USD: float = Field(50.0)
    RISK_FEATURE_SCAN_TIER_3_SECONDS: int = Field(16)
    RISK_FEATURE_SCAN_TIER_4_USD: float = Field(25.0)
    RISK_FEATURE_SCAN_TIER_4_SECONDS: int = Field(32)
    RISK_FEATURE_SCAN_TIER_5_SECONDS: int = Field(64)

    TOP1_HOLDER_SCAN_TIER_1_USD: float = Field(150.0)
    TOP1_HOLDER_SCAN_TIER_1_SECONDS: int = Field(20)
    TOP1_HOLDER_SCAN_TIER_2_USD: float = Field(100.0)
    TOP1_HOLDER_SCAN_TIER_2_SECONDS: int = Field(30)
    TOP1_HOLDER_SCAN_TIER_3_USD: float = Field(50.0)
    TOP1_HOLDER_SCAN_TIER_3_SECONDS: int = Field(60)
    TOP1_HOLDER_SCAN_TIER_4_USD: float = Field(25.0)
    TOP1_HOLDER_SCAN_TIER_4_SECONDS: int = Field(120)
    TOP1_HOLDER_SCAN_TIER_5_SECONDS: int = Field(0)

    DUST_FORCE_EXIT_USD: float = Field(12.5)
    DUST_FORCE_EXIT_SOL: float = Field(0.125)

    WALLET_PUBLIC_KEY: Optional[str] = None
    WALLET_PRIVATE_KEY_BASE58: Optional[SecretStr] = None

    def _env_value(self, key: str) -> str:
        value = os.environ.get(key)
        if value is None and hasattr(self, key):
            value = getattr(self, key)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        return str(value or "").strip()

    def _csv_env_values(self, key: str) -> List[str]:
        return _split_csv(self._env_value(key))

    def _indexed_keys(self) -> List[str]:
        keys = set(os.environ.keys())
        try:
            keys.update(self.__class__.model_fields.keys())
        except Exception:
            pass
        return sorted(keys)

    def _numbered_indices(self, prefix: str, suffix: Optional[str] = None, max_items: Optional[int] = None) -> List[int]:
        indices = set()
        escaped_prefix = re.escape(prefix)
        if suffix is None:
            patterns = [re.compile(rf"^{escaped_prefix}_(\d+)$")]
        else:
            escaped_suffix = re.escape(suffix)
            patterns = [
                re.compile(rf"^{escaped_prefix}_(\d+)_{escaped_suffix}$"),
                re.compile(rf"^{escaped_prefix}_{escaped_suffix}_(\d+)$"),
            ]
        for key in self._indexed_keys():
            for pattern in patterns:
                m = pattern.match(key)
                if not m:
                    continue
                idx = int(m.group(1))
                if max_items is None or idx <= int(max_items):
                    indices.add(idx)
        return sorted(indices)

    def _scan_numbered_secrets(self, prefix: str, suffix: Optional[str] = None, max_items: Optional[int] = None) -> List[str]:
        values: List[str] = []
        for i in self._numbered_indices(prefix, suffix, max_items=max_items):
            keys = [f"{prefix}_{i}"] if suffix is None else [f"{prefix}_{i}_{suffix}", f"{prefix}_{suffix}_{i}"]
            for key in keys:
                value = self._env_value(key)
                if value:
                    values.append(value)
                    break
        return values

    def _scan_gmgn_accounts(self, max_items: Optional[int] = None) -> List[dict]:
        env_keys = os.environ.keys()
        accounts_by_index: Dict[int, dict] = {}
        for field, values in (
            ("api_key", self._csv_env_values("GMGN_API_KEY")),
            ("public_key", self._csv_env_values("GMGN_PUBLIC_KEY")),
            ("client_id", self._csv_env_values("GMGN_CLIENT_ID")),
            ("private_key", self._csv_env_values("GMGN_PRIVATE_KEY")),
        ):
            for idx, value in enumerate(values, start=1):
                if value:
                    accounts_by_index.setdefault(idx, {"index": idx})[field] = value
        indexed_fields: Dict[int, Dict[str, str]] = {}
        for key in sorted(set(env_keys) | set(self._indexed_keys())):
            for pattern in (
                re.compile(r"^GMGN_(\d+)_(API_KEY|CLIENT_ID|PUBLIC_KEY|PRIVATE_KEY)$"),
                re.compile(r"^GMGN_(API_KEY|CLIENT_ID|PUBLIC_KEY|PRIVATE_KEY)_(\d+)$"),
            ):
                m = pattern.match(key)
                if not m:
                    continue
                idx = int(m.group(1) if m.group(1).isdigit() else m.group(2))
                if max_items is not None and idx > int(max_items):
                    continue
                field = (m.group(2) if m.group(1).isdigit() else m.group(1)).lower()
                value = self._env_value(key)
                if value:
                    indexed_fields.setdefault(idx, {})[field] = value
        for idx, values in indexed_fields.items():
            account = accounts_by_index.setdefault(idx, {"index": idx})
            for field, value in values.items():
                account.setdefault(field, value)
        accounts: List[dict] = []
        for idx in sorted(accounts_by_index):
            raw = accounts_by_index[idx]
            api_key = raw.get("api_key", "")
            public_key = raw.get("public_key", "")
            client_id = raw.get("client_id", "") or public_key
            private_key = raw.get("private_key", "")
            if api_key or client_id or private_key:
                accounts.append({
                    "index": idx, "api_key": api_key, "client_id": client_id,
                    "public_key": public_key, "private_key": private_key,
                })
        return accounts

    def get_gmgn_accounts(self) -> List[dict]:
        return self._scan_gmgn_accounts()

    def get_gmgn_api_keys(self) -> List[str]:
        keys = [a.get("api_key", "") for a in self.get_gmgn_accounts() if a.get("api_key")]
        return keys or self._scan_numbered_secrets("GMGN", "API_KEY")

    def get_gmgn_api_key(self) -> Optional[str]:
        keys = self.get_gmgn_api_keys()
        return keys[0] if keys else None

    def get_gmgn_client_ids(self) -> List[str]:
        return [a.get("client_id", "") for a in self.get_gmgn_accounts() if a.get("client_id")]

    def get_gmgn_private_keys(self) -> List[str]:
        keys = [a.get("private_key", "") for a in self.get_gmgn_accounts() if a.get("private_key")]
        return keys or self._scan_numbered_secrets("GMGN", "PRIVATE_KEY")

    def get_gmgn_credentials(self) -> List[dict]:
        accounts = self.get_gmgn_accounts()
        if accounts:
            return accounts
        api_keys = self._scan_numbered_secrets("GMGN", "API_KEY")
        private_keys = self._scan_numbered_secrets("GMGN", "PRIVATE_KEY")
        return [
            {"index": idx + 1, "api_key": api_key, "client_id": "", "public_key": "",
             "private_key": private_keys[idx] if idx < len(private_keys) else ""}
            for idx, api_key in enumerate(api_keys)
        ]

    def get_gmgn_credential_count(self) -> int:
        return len(self.get_gmgn_credentials())

    def _scan_jupiter_api_keys(self) -> List[str]:
        env_keys = os.environ.keys()
        values = self._csv_env_values("JUPITER_API_KEY")
        values.extend(self._scan_numbered_secrets("JUPITER_API_KEY"))
        for key in env_keys:
            if key == "JUPITER_API_KEY":
                continue
        return _dedupe(values)

    def _scan_ankr_api_keys(self) -> List[str]:
        env_keys = os.environ.keys()
        values = self._csv_env_values("ANKR_API_KEY")
        values.extend(self._scan_numbered_secrets("ANKR_API_KEY"))
        for key in env_keys:
            if key == "ANKR_API_KEY":
                continue
        return _dedupe(values)

    def get_gmgn_kline_path(self) -> str:
        return (self.GMGN_KLINE_PATH or self.GMGN_TOKEN_KLINE_PATH or "/v1/market/token_kline").strip()

    def get_jupiter_api_keys(self) -> List[SecretStr]:
        keys: List[SecretStr] = []
        for raw in self._scan_jupiter_api_keys():
            if raw:
                keys.append(SecretStr(raw))
        existing = {_secret_to_str(k) for k in keys}
        for item in [self.JUPITER_API_KEY_MEME1, self.JUPITER_API_KEY_MEME2, self.JUPITER_API_KEY_MEME3]:
            raw = _secret_to_str(item)
            if raw and raw not in existing:
                keys.append(SecretStr(raw))
                existing.add(raw)
        return keys

    def get_jupiter_api_key(self) -> Optional[SecretStr]:
        keys = self.get_jupiter_api_keys()
        return keys[0] if keys else None

    def get_jupiter_api_base_url(self) -> str:
        return (self.JUPITER_API_BASE_URL or self.JUPITER_API_BASE or "https://api.jup.ag/swap/v1").rstrip("/")

    def get_ankr_api_keys(self) -> List[SecretStr]:
        return [SecretStr(v) for v in self._scan_ankr_api_keys() if v]

    def get_alchemy_api_keys(self) -> List[SecretStr]:
        keys = [SecretStr(v) for v in self._scan_numbered_secrets("ALCHEMY_API_KEY") if v]
        existing = {_secret_to_str(k) for k in keys}
        for raw in _split_csv(self.ALCHEMY_API_KEYS):
            if raw and raw not in existing:
                keys.append(SecretStr(raw))
                existing.add(raw)
        return keys

    def _get_ankr_http_url(self, api_key: Optional[SecretStr]) -> Optional[str]:
        raw = _secret_to_str(api_key)
        return f"https://rpc.ankr.com/solana/{raw}" if raw else None

    def _get_alchemy_http_url(self, api_key: Optional[SecretStr]) -> Optional[str]:
        raw = _secret_to_str(api_key)
        return f"https://solana-mainnet.g.alchemy.com/v2/{raw}" if raw else None

    def get_rpc_http_urls(self) -> List[str]:
        urls: List[str] = []
        urls.extend(_split_csv(self.SOLANA_RPC_HTTP_URLS))
        urls.extend(_split_csv(self.SOLANA_RPC_URL))
        for item in [self.SOLANA_RPC_HTTP_PRIMARY, self.SOLANA_RPC_HTTP_BACKUP_1]:
            if not item:
                continue
            val = item.strip()
            if val.lower() == "alchemy":
                urls.extend(filter(None, [self._get_alchemy_http_url(k) for k in self.get_alchemy_api_keys()]))
            elif val.lower() == "ankr":
                urls.extend(filter(None, [self._get_ankr_http_url(k) for k in self.get_ankr_api_keys()]))
            else:
                urls.append(val)
        urls.extend(filter(None, [self._get_alchemy_http_url(k) for k in self.get_alchemy_api_keys()]))
        urls.extend(filter(None, [self._get_ankr_http_url(k) for k in self.get_ankr_api_keys()]))
        return _dedupe(urls)

    def get_rpc_http_url(self) -> Optional[str]:
        urls = self.get_rpc_http_urls()
        return urls[0] if urls else None

    def get_rpc_ws_url(self) -> Optional[str]:
        if self.SOLANA_RPC_WS_PRIMARY:
            return self.SOLANA_RPC_WS_PRIMARY.strip()
        if self.SOLANA_RPC_WS_BACKUP_1:
            return self.SOLANA_RPC_WS_BACKUP_1.strip()
        return None

    def get_rpc_url(self) -> Optional[str]:
        return self.get_rpc_http_url()

    def get_wallet_public_key(self) -> Optional[str]:
        return (self.WALLET_PUBLIC_KEY or self.WALLET_ADDRESS or "").strip() or None

    def get_wallet_private_key_base58(self) -> Optional[str]:
        return _secret_to_str(self.WALLET_PRIVATE_KEY_BASE58) or _secret_to_str(self.PRIVATE_KEY)

    @staticmethod
    def _tiered_interval(value: Optional[float], tiers: List[tuple[float, int]], fallback_seconds: int) -> int:
        try:
            v = max(float(value or 0.0), 0.0)
        except (TypeError, ValueError):
            v = 0.0
        for threshold, seconds in tiers:
            if v >= float(threshold):
                return max(int(seconds), 0)
        return max(int(fallback_seconds), 0)

    def get_risk_scan_interval_seconds(self, remaining_value_usd: Optional[float] = None) -> int:
        return self._tiered_interval(
            remaining_value_usd,
            [
                (self.RISK_FEATURE_SCAN_TIER_1_USD, self.RISK_FEATURE_SCAN_TIER_1_SECONDS),
                (self.RISK_FEATURE_SCAN_TIER_2_USD, self.RISK_FEATURE_SCAN_TIER_2_SECONDS),
                (self.RISK_FEATURE_SCAN_TIER_3_USD, self.RISK_FEATURE_SCAN_TIER_3_SECONDS),
                (self.RISK_FEATURE_SCAN_TIER_4_USD, self.RISK_FEATURE_SCAN_TIER_4_SECONDS),
            ],
            self.RISK_FEATURE_SCAN_TIER_5_SECONDS,
        )

    def get_top1_holder_scan_interval_seconds(self, remaining_value_usd: Optional[float] = None) -> Optional[int]:
        seconds = self._tiered_interval(
            remaining_value_usd,
            [
                (self.TOP1_HOLDER_SCAN_TIER_1_USD, self.TOP1_HOLDER_SCAN_TIER_1_SECONDS),
                (self.TOP1_HOLDER_SCAN_TIER_2_USD, self.TOP1_HOLDER_SCAN_TIER_2_SECONDS),
                (self.TOP1_HOLDER_SCAN_TIER_3_USD, self.TOP1_HOLDER_SCAN_TIER_3_SECONDS),
                (self.TOP1_HOLDER_SCAN_TIER_4_USD, self.TOP1_HOLDER_SCAN_TIER_4_SECONDS),
            ],
            self.TOP1_HOLDER_SCAN_TIER_5_SECONDS,
        )
        return seconds if seconds > 0 else None

    def get_provider_mode(self) -> ProviderMode:
        if self.PROVIDER_MODE is not None:
            return self.PROVIDER_MODE
        if self.LIVE_TRADING_ENABLED is True:
            return ProviderMode.LIVE
        if self.DRY_RUN:
            return ProviderMode.MOCK
        return ProviderMode.LIVE

    def set_provider_mode(self, mode: ProviderMode | str | None) -> None:
        if mode is None:
            self.PROVIDER_MODE = None
            return
        self.PROVIDER_MODE = mode if isinstance(mode, ProviderMode) else ProviderMode(str(mode))

    def mask_key(self, s: Optional[SecretStr | str]) -> Optional[str]:
        val = _secret_to_str(s)
        if val is None:
            return None
        return "****" if len(val) <= 8 else val[:4] + "..." + val[-4:]

    @model_validator(mode="after")
    def validate_live_config(self):
        if not self.GMGN_TOKEN_KLINE_PATH:
            self.GMGN_TOKEN_KLINE_PATH = self.GMGN_KLINE_PATH
        elif not self.GMGN_KLINE_PATH:
            self.GMGN_KLINE_PATH = self.GMGN_TOKEN_KLINE_PATH
        mode = self.get_provider_mode()
        if mode == ProviderMode.LIVE:
            missing: List[str] = []
            if not self.GMGN_API_BASE_URL:
                missing.append("GMGN_API_BASE_URL")
            if not (self.get_gmgn_api_keys() or self.get_gmgn_client_ids() or self.get_gmgn_credentials()):
                missing.append("GMGN_API_KEY or GMGN_CLIENT_ID/GMGN_PUBLIC_KEY")
            if not self.get_jupiter_api_base_url():
                missing.append("JUPITER_API_BASE_URL")
            if not self.get_jupiter_api_keys():
                missing.append("JUPITER_API_KEY")
            if not self.get_rpc_http_urls():
                missing.append("SOLANA_RPC_HTTP_URLS or ALCHEMY_API_KEY")
            if not self.JITO_ENABLED:
                missing.append("JITO_ENABLED")
            if not self.JITO_BLOCK_ENGINE_URL:
                missing.append("JITO_BLOCK_ENGINE_URL")
            if not self.get_wallet_public_key():
                missing.append("WALLET_PUBLIC_KEY")
            if not self.get_wallet_private_key_base58():
                missing.append("WALLET_PRIVATE_KEY_BASE58")
            if missing:
                warnings.warn("PROVIDER_MODE=live is configured but live trading is not ready: " + ", ".join(missing))
        elif mode == ProviderMode.ONLINE_READONLY and self.get_wallet_private_key_base58():
            warnings.warn("WALLET_PRIVATE_KEY_BASE58/PRIVATE_KEY is set but PROVIDER_MODE=online_readonly. Private key will NOT be used.")
        return self


settings = Settings()
