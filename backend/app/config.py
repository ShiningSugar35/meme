from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr, model_validator
from typing import Optional, List, Dict, Any
import os
import re
import warnings
from enum import Enum

# Pre-load .env into os.environ so dynamic key scanning can find ALL keys,
# not just the ones explicitly defined as model fields.
# This enables truly dynamic GMGN_API_KEY_N / JUPITER_API_KEY_N /
# ANKR_API_KEY_N / ALCHEMY_API_KEY_N without a hardcoded practical limit.
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
    """Return a stripped string from SecretStr/str, treating blanks as None."""
    if value is None:
        return None
    if hasattr(value, "get_secret_value"):
        raw = value.get_secret_value()
    else:
        raw = str(value)
    raw = (raw or "").strip()
    return raw or None


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item and item.strip()]


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    APP_ENV: str = Field('development')
    SQLITE_PATH: str = Field('./data/trading_bot.sqlite3')

    # Provider Mode (new, preferred)
    PROVIDER_MODE: Optional[ProviderMode] = Field(
        None,
        description="Provider mode: mock/online_readonly/live. Overrides DRY_RUN if set.",
    )

    # DRY_RUN MODE (legacy, kept for compatibility)
    DRY_RUN: bool = Field(
        True,
        description="DRY_RUN=true blocks real transactions. Always true by default for safety.",
    )

    # Legacy env names kept only so old front-end/backend checks do not become disconnected.
    LIVE_TRADING_ENABLED: Optional[bool] = None
    PRIVATE_KEY: Optional[SecretStr] = None
    WALLET_ADDRESS: Optional[str] = None
    SOLANA_RPC_URL: Optional[str] = None
    JUPITER_API_BASE: Optional[str] = None

    SIMULATION_ENABLED: bool = Field(True)

    # GMGN API Configuration
    GMGN_API_BASE_URL: Optional[str] = None
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

    GMGN_TRENCHES_PATH: str = Field('/api/v1/trenches')
    GMGN_TRENCHES_METHOD: str = Field('POST')
    GMGN_TRENCHES_TYPES: str = Field('new_creation')
    GMGN_TRENCHES_PLATFORMS: str = Field('')
    GMGN_TOKEN_PRICE_PATH: str = Field('/v1/token/info')
    GMGN_KLINE_PATH: str = Field('/v1/market/token_kline')
    GMGN_TOKEN_INFO_PATH: str = Field('/v1/token/info')
    GMGN_TOKEN_SECURITY_PATH: str = Field('/v1/token/security')
    GMGN_TOKEN_POOL_INFO_PATH: str = Field('/v1/token/pool_info')
    GMGN_TOKEN_HOLDERS_PATH: str = Field('/v1/market/token_top_holders')

    # Jupiter API Configuration
    JUPITER_API_BASE_URL: Optional[str] = None
    JUPITER_API_KEY_1: Optional[SecretStr] = None
    JUPITER_API_KEY_2: Optional[SecretStr] = None
    JUPITER_API_KEY_3: Optional[SecretStr] = None
    JUPITER_API_KEY_MEME1: Optional[SecretStr] = None
    JUPITER_API_KEY_MEME2: Optional[SecretStr] = None
    JUPITER_API_KEY_MEME3: Optional[SecretStr] = None

    # Solana RPC Configuration: HTTP-only load balancing is supported.
    # No Alchemy WebSocket field is required or constructed.
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

    # Jito Configuration
    JITO_ENABLED: bool = Field(True)
    JITO_BLOCK_ENGINE_URL: str = Field('https://mainnet.block-engine.jito.wtf')
    JITO_TIP_FLOOR_URL: str = Field('https://bundles.jito.wtf/api/v1/bundles/tip_floor')
    JITO_TIP_STREAM_WS: str = Field('wss://bundles.jito.wtf/api/v1/bundles/tip_stream')

    # Trading Parameters
    POLL_INTERVAL_SECONDS: int = Field(60)
    ACTIVE_POSITION_PRICE_POLL_SECONDS: int = Field(1)
    TIP_FLOOR_REFRESH_SECONDS: int = Field(3)

    BUY_SLIPPAGE_CAP_BPS: int = Field(1500)
    SELL_SLIPPAGE_CAP_BPS: int = Field(2000)
    EMERGENCY_SLIPPAGE_CAP_BPS: int = Field(3500)
    PRICE_IMPACT_HARD_CAP_PCT: float = Field(10.0)

    LIVE_ROLLING_10_LOSS_LIMIT: float = Field(-0.20)
    MAX_REQUOTE_RETRY: int = Field(2)

    # Entry sizing (USD based): min(liquidity_usd * pct, max_usd)
    ENTRY_SIZE_LIQUIDITY_PCT: float = Field(0.015)
    ENTRY_MAX_USD: float = Field(200.0)

    # Risk Feature Scan Tiers (dynamic based on remaining position value in USD)
    # Strategy: >=$150 every 2s; >=$100 every 3s; >=$50 every 6s;
    # >=$25 every 12s; <$25 every 24s.
    RISK_FEATURE_SCAN_TIER_1_USD: float = Field(150.0)
    RISK_FEATURE_SCAN_TIER_1_SECONDS: int = Field(2)
    RISK_FEATURE_SCAN_TIER_2_USD: float = Field(100.0)
    RISK_FEATURE_SCAN_TIER_2_SECONDS: int = Field(3)
    RISK_FEATURE_SCAN_TIER_3_USD: float = Field(50.0)
    RISK_FEATURE_SCAN_TIER_3_SECONDS: int = Field(6)
    RISK_FEATURE_SCAN_TIER_4_USD: float = Field(25.0)
    RISK_FEATURE_SCAN_TIER_4_SECONDS: int = Field(12)
    RISK_FEATURE_SCAN_TIER_5_SECONDS: int = Field(24)

    # Top1 normal-holder scan tiers (USD based). 0 disables scanning below tier 4.
    TOP1_HOLDER_SCAN_TIER_1_USD: float = Field(150.0)
    TOP1_HOLDER_SCAN_TIER_1_SECONDS: int = Field(10)
    TOP1_HOLDER_SCAN_TIER_2_USD: float = Field(100.0)
    TOP1_HOLDER_SCAN_TIER_2_SECONDS: int = Field(15)
    TOP1_HOLDER_SCAN_TIER_3_USD: float = Field(50.0)
    TOP1_HOLDER_SCAN_TIER_3_SECONDS: int = Field(30)
    TOP1_HOLDER_SCAN_TIER_4_USD: float = Field(25.0)
    TOP1_HOLDER_SCAN_TIER_4_SECONDS: int = Field(60)
    TOP1_HOLDER_SCAN_TIER_5_SECONDS: int = Field(0)

    # Dust Position Rules (USD based; SOL kept as legacy fallback only)
    DUST_FORCE_EXIT_USD: float = Field(12.5)
    DUST_FORCE_EXIT_SOL: float = Field(0.15)

    # Legacy SOL-tier fields retained only so older modules do not break if imported.
    RISK_FEATURE_SCAN_TIER_1_SOL: float = Field(1.5)
    RISK_FEATURE_SCAN_TIER_2_SOL: float = Field(1.0)
    RISK_FEATURE_SCAN_TIER_3_SOL: float = Field(0.5)
    RISK_FEATURE_SCAN_TIER_4_SOL: float = Field(0.25)

    # Wallet Configuration
    WALLET_PUBLIC_KEY: Optional[str] = None
    WALLET_PRIVATE_KEY_BASE58: Optional[SecretStr] = None

    def _env_value(self, key: str) -> str:
        """Return a real secret string from os.environ or a Settings field."""
        value = os.environ.get(key)
        if value is None and hasattr(self, key):
            value = getattr(self, key)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        return str(value or '').strip()

    def _numbered_indices(self, prefix: str, suffix: Optional[str] = None, max_items: int = 12) -> List[int]:
        """Return all configured numeric suffixes, including keys only present in .env/os.environ."""
        indices = set(range(1, max_items + 1))
        escaped_prefix = re.escape(prefix)
        if suffix is None:
            patterns = [re.compile(rf"^{escaped_prefix}_(\d+)$")]
        else:
            escaped_suffix = re.escape(suffix)
            patterns = [
                re.compile(rf"^{escaped_prefix}_(\d+)_{escaped_suffix}$"),
                re.compile(rf"^{escaped_prefix}_{escaped_suffix}_(\d+)$"),
            ]
        for key in os.environ.keys():
            for pattern in patterns:
                m = pattern.match(key)
                if m:
                    indices.add(int(m.group(1)))
        return sorted(indices)

    def _scan_numbered_secrets(self, prefix: str, suffix: Optional[str] = None, max_items: int = 12) -> List[str]:
        """Scan numbered secrets in both legacy and new naming conventions.

        - _scan_numbered_secrets('ANKR_API_KEY') -> ANKR_API_KEY_1..N
        - _scan_numbered_secrets('GMGN', 'API_KEY') -> GMGN_1_API_KEY and GMGN_API_KEY_1

        The first 12 slots are model fields for backwards compatibility, but
        higher numbered values are also discovered from .env/os.environ.
        """
        values: List[str] = []
        for i in self._numbered_indices(prefix, suffix, max_items=max_items):
            keys = [f'{prefix}_{i}'] if suffix is None else [f'{prefix}_{i}_{suffix}', f'{prefix}_{suffix}_{i}']
            for key in keys:
                value = self._env_value(key)
                if value:
                    values.append(value)
                    break
        return values

    def _scan_gmgn_accounts(self, max_items: int = 12) -> List[dict]:
        """Return per-account GMGN credentials.

        GMGN deployments in the wild use different auth names.  The project
        originally only sent X-API-Key/X-Route-Key; the current OpenAPI error
        explicitly mentions "api key or client_id", so we keep both api_key and
        client_id per numbered account and let the provider send them through
        headers plus query/body auth fields.
        """
        indices = set(range(1, max_items + 1))
        for key in os.environ.keys():
            for pattern in (
                re.compile(r"^GMGN_(\d+)_(API_KEY|CLIENT_ID|PUBLIC_KEY|PRIVATE_KEY)$"),
                re.compile(r"^GMGN_(API_KEY|CLIENT_ID|PUBLIC_KEY|PRIVATE_KEY)_(\d+)$"),
            ):
                m = pattern.match(key)
                if m:
                    idx = m.group(1) if m.group(1).isdigit() else m.group(2)
                    indices.add(int(idx))
        accounts: List[dict] = []
        for i in sorted(indices):
            api_key = self._env_value(f'GMGN_{i}_API_KEY') or self._env_value(f'GMGN_API_KEY_{i}')
            public_key = self._env_value(f'GMGN_{i}_PUBLIC_KEY') or self._env_value(f'GMGN_PUBLIC_KEY_{i}')
            client_id = (
                self._env_value(f'GMGN_{i}_CLIENT_ID')
                or self._env_value(f'GMGN_CLIENT_ID_{i}')
                or public_key
            )
            private_key = self._env_value(f'GMGN_{i}_PRIVATE_KEY') or self._env_value(f'GMGN_PRIVATE_KEY_{i}')
            if api_key or client_id or private_key:
                accounts.append({
                    'index': i,
                    'api_key': api_key,
                    'client_id': client_id,
                    'public_key': public_key,
                    'private_key': private_key,
                })
        return accounts

    def get_gmgn_accounts(self) -> List[dict]:
        return self._scan_gmgn_accounts()

    def get_gmgn_api_keys(self) -> List[str]:
        keys = [a.get('api_key', '') for a in self.get_gmgn_accounts() if a.get('api_key')]
        if not keys:
            # Backward compatibility with older .env examples.
            keys = self._scan_numbered_secrets('GMGN', 'API_KEY')
        return keys

    def get_gmgn_client_ids(self) -> List[str]:
        return [a.get('client_id', '') for a in self.get_gmgn_accounts() if a.get('client_id')]

    def get_gmgn_private_keys(self) -> List[str]:
        keys = [a.get('private_key', '') for a in self.get_gmgn_accounts() if a.get('private_key')]
        if not keys:
            keys = self._scan_numbered_secrets('GMGN', 'PRIVATE_KEY')
        return keys

    def get_gmgn_credentials(self) -> List[dict]:
        accounts = self.get_gmgn_accounts()
        if accounts:
            return accounts
        api_keys = self._scan_numbered_secrets('GMGN', 'API_KEY')
        private_keys = self._scan_numbered_secrets('GMGN', 'PRIVATE_KEY')
        return [
            {'index': idx + 1, 'api_key': api_key, 'client_id': '', 'public_key': '', 'private_key': private_keys[idx] if idx < len(private_keys) else ''}
            for idx, api_key in enumerate(api_keys)
        ]


    def get_jupiter_api_keys(self) -> List[SecretStr]:
        keys: List[SecretStr] = []
        for raw in self._scan_numbered_secrets('JUPITER_API_KEY'):
            if raw:
                keys.append(SecretStr(raw))
        legacy = [self.JUPITER_API_KEY_MEME1, self.JUPITER_API_KEY_MEME2, self.JUPITER_API_KEY_MEME3]
        existing_values = {_secret_to_str(k) for k in keys}
        for item in legacy:
            raw = _secret_to_str(item)
            if raw and raw not in existing_values:
                keys.append(SecretStr(raw))
                existing_values.add(raw)
        return keys

    def get_jupiter_api_key(self) -> Optional[SecretStr]:
        keys = self.get_jupiter_api_keys()
        return keys[0] if keys else None

    def get_jupiter_api_base_url(self) -> str:
        return (self.JUPITER_API_BASE_URL or self.JUPITER_API_BASE or 'https://api.jup.ag/swap/v1').rstrip('/')

    def get_ankr_api_keys(self) -> List[SecretStr]:
        return self._scan_numbered_secrets('ANKR_API_KEY')

    def get_alchemy_api_keys(self) -> List[SecretStr]:
        keys = self._scan_numbered_secrets('ALCHEMY_API_KEY')
        extra = _split_csv(self.ALCHEMY_API_KEYS)
        existing_values = {_secret_to_str(k) for k in keys}
        for raw in extra:
            if raw and raw not in existing_values:
                keys.append(SecretStr(raw))
                existing_values.add(raw)
        return keys

    def _get_ankr_http_url(self, api_key: Optional[SecretStr]) -> Optional[str]:
        raw = _secret_to_str(api_key)
        return f"https://rpc.ankr.com/solana/{raw}" if raw else None

    def _get_alchemy_http_url(self, api_key: Optional[SecretStr]) -> Optional[str]:
        raw = _secret_to_str(api_key)
        return f"https://solana-mainnet.g.alchemy.com/v2/{raw}" if raw else None

    def get_rpc_http_urls(self) -> List[str]:
        """
        Return HTTP RPC endpoints for load balancing.

        Order:
        1. SOLANA_RPC_HTTP_URLS CSV (preferred for your Alchemy four-endpoint setup)
        2. Legacy SOLANA_RPC_URL CSV
        3. SOLANA_RPC_HTTP_PRIMARY / SOLANA_RPC_HTTP_BACKUP_1 direct URLs
        4. Constructed Alchemy endpoints from ALCHEMY_API_KEY_N / ALCHEMY_API_KEYS
        5. Constructed Ankr endpoints as last fallback
        """
        urls: List[str] = []
        urls.extend(_split_csv(self.SOLANA_RPC_HTTP_URLS))
        urls.extend(_split_csv(self.SOLANA_RPC_URL))

        for item in [self.SOLANA_RPC_HTTP_PRIMARY, self.SOLANA_RPC_HTTP_BACKUP_1]:
            if not item:
                continue
            item = item.strip()
            if not item:
                continue
            if item.lower() == 'alchemy':
                urls.extend(filter(None, [self._get_alchemy_http_url(k) for k in self.get_alchemy_api_keys()]))
            elif item.lower() == 'ankr':
                urls.extend(filter(None, [self._get_ankr_http_url(k) for k in self.get_ankr_api_keys()]))
            else:
                urls.append(item)

        urls.extend(filter(None, [self._get_alchemy_http_url(k) for k in self.get_alchemy_api_keys()]))
        urls.extend(filter(None, [self._get_ankr_http_url(k) for k in self.get_ankr_api_keys()]))
        return _dedupe(urls)

    def get_rpc_http_url(self) -> Optional[str]:
        urls = self.get_rpc_http_urls()
        return urls[0] if urls else None

    def get_rpc_ws_url(self) -> Optional[str]:
        """Return only explicitly configured WebSocket URLs. Alchemy free HTTP-only plan needs none."""
        if self.SOLANA_RPC_WS_PRIMARY:
            return self.SOLANA_RPC_WS_PRIMARY.strip()
        if self.SOLANA_RPC_WS_BACKUP_1:
            return self.SOLANA_RPC_WS_BACKUP_1.strip()
        return None

    def get_rpc_url(self) -> Optional[str]:
        """Legacy method for backward compatibility. Use get_rpc_http_url(s) instead."""
        return self.get_rpc_http_url()

    def get_wallet_public_key(self) -> Optional[str]:
        return (self.WALLET_PUBLIC_KEY or self.WALLET_ADDRESS or '').strip() or None

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

    def get_risk_scan_interval_seconds(
        self,
        remaining_value_usd: Optional[float] = None,
        remaining_value_sol: Optional[float] = None,
    ) -> int:
        """Return the post-entry core-risk scan interval.

        The current strategy is USD-denominated. ``remaining_value_sol`` is accepted
        only for backwards-compatible call sites; it is not used for tiering unless
        USD is absent and old code explicitly handles conversion before calling.
        """
        _ = remaining_value_sol
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

    def get_top1_holder_scan_interval_seconds(
        self,
        remaining_value_usd: Optional[float] = None,
        remaining_value_sol: Optional[float] = None,
    ) -> Optional[int]:
        """Return the Top1 addr_type=0 holder scan interval, or None when disabled."""
        _ = remaining_value_sol
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
        """Get effective provider mode, considering new and legacy switches."""
        if self.PROVIDER_MODE is not None:
            return self.PROVIDER_MODE
        if self.LIVE_TRADING_ENABLED is True:
            return ProviderMode.LIVE
        if self.DRY_RUN:
            return ProviderMode.MOCK
        return ProviderMode.LIVE

    def mask_key(self, s: Optional[SecretStr | str]) -> Optional[str]:
        val = _secret_to_str(s)
        if val is None:
            return None
        if len(val) <= 8:
            return '****'
        return val[:4] + '...' + val[-4:]

    @model_validator(mode='after')
    def validate_live_config(self):
        """
        Do not raise during app boot.

        The runtime mode switch uses /api/runtime/status and /api/runtime/mode as the
        authoritative gate for real live trading. Raising here makes the UI unusable
        when a user is still filling .env values.
        """
        mode = self.get_provider_mode()

        if mode == ProviderMode.LIVE:
            missing: List[str] = []
            if not self.GMGN_API_BASE_URL:
                missing.append('GMGN_API_BASE_URL')
            if not (self.get_gmgn_api_keys() or self.get_gmgn_client_ids() or self.get_gmgn_credentials()):
                missing.append('GMGN_API_KEY_N or GMGN_CLIENT_ID_N/GMGN_PUBLIC_KEY_N')
            if not self.get_jupiter_api_base_url():
                missing.append('JUPITER_API_BASE_URL')
            if not self.get_jupiter_api_keys():
                missing.append('JUPITER_API_KEY_N')
            if not self.get_rpc_http_urls():
                missing.append('SOLANA_RPC_HTTP_URLS or ALCHEMY_API_KEY_N')
            if not self.JITO_ENABLED:
                missing.append('JITO_ENABLED')
            if not self.JITO_BLOCK_ENGINE_URL:
                missing.append('JITO_BLOCK_ENGINE_URL')
            if not self.get_wallet_public_key():
                missing.append('WALLET_PUBLIC_KEY')
            if not self.get_wallet_private_key_base58():
                missing.append('WALLET_PRIVATE_KEY_BASE58')
            if missing:
                warnings.warn(
                    'PROVIDER_MODE=live is configured but live trading is not ready: '
                    + ', '.join(missing)
                )

        elif mode == ProviderMode.ONLINE_READONLY:
            if self.get_wallet_private_key_base58():
                warnings.warn(
                    'WALLET_PRIVATE_KEY_BASE58/PRIVATE_KEY is set but '
                    'PROVIDER_MODE=online_readonly. Private key will NOT be used.'
                )

        return self


settings = Settings()
