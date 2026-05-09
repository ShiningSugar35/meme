from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr, model_validator
from typing import Optional, List, Dict, Any
import os
import re
from enum import Enum


class ProviderMode(str, Enum):
    MOCK = "mock"
    ONLINE_READONLY = "online_readonly"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8')

    APP_ENV: str = Field('development')
    SQLITE_PATH: str = Field('./data/trading_bot.sqlite3')

    # Provider Mode (new, preferred)
    PROVIDER_MODE: Optional[ProviderMode] = Field(None, description="Provider mode: mock/online_readonly/live. Overrides DRY_RUN if set.")

    # DRY_RUN MODE (legacy, kept for compatibility)
    DRY_RUN: bool = Field(True, description="DRY_RUN=true blocks real transactions. Always true by default for safety.")

    LIVE_TRADING_ENABLED: bool = Field(False, description="Must be true for any live trading. Independent of DRY_RUN.")

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
    # Support for additional GMGN keys (4-12+)
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
    
    GMGN_TRENCHES_PATH: str = Field('/api/v1/trenches')
    GMGN_TOKEN_PRICE_PATH: str = Field('/api/v1/token/price')
    GMGN_KLINE_PATH: str = Field('/api/v1/token/kline')
    
    def _scan_gmgn_accounts(self) -> List[Dict[str, Any]]:
        """Dynamically scan for GMGN_API_KEY_* from model fields + os.environ fallback."""
        accounts = []
        api_keys = {}
        seen_indices: set = set()

        # 1. Scan explicitly defined model fields (1-12)
        for i in range(1, 13):
            key_field = getattr(self, f'GMGN_API_KEY_{i}', None)
            if key_field is not None:
                secret_val = key_field.get_secret_value() if hasattr(key_field, 'get_secret_value') else key_field
                if secret_val and isinstance(secret_val, str) and secret_val.strip():
                    api_keys[i] = key_field
                    seen_indices.add(i)

        # 2. Also scan os.environ for keys beyond 12 (future expansion)
        for key, value in os.environ.items():
            match = re.match(r'GMGN_API_KEY_(\d+)', key)
            if match and value:
                index = int(match.group(1))
                if index not in seen_indices:
                    api_keys[index] = SecretStr(value)
                    seen_indices.add(index)

        # For each API key found, assemble account with public/private keys
        for index in sorted(api_keys.keys()):
            account = {'index': index, 'api_key': api_keys[index]}

            # Try model field first, then os.environ fallback
            pub_key = getattr(self, f'GMGN_PUBLIC_KEY_{index}', None) or os.environ.get(f'GMGN_PUBLIC_KEY_{index}')
            priv_key = getattr(self, f'GMGN_PRIVATE_KEY_{index}', None)
            priv_key_env = os.environ.get(f'GMGN_PRIVATE_KEY_{index}')

            if priv_key is None and priv_key_env:
                priv_key = SecretStr(priv_key_env)

            # Validate public/private key pairing
            if (pub_key and not priv_key) or (not pub_key and priv_key):
                account['invalid_config'] = f"Mismatched keys: public_key={bool(pub_key)}, private_key={bool(priv_key)}"
            else:
                account['public_key'] = pub_key
                account['private_key'] = priv_key if priv_key else None

            accounts.append(account)

        return accounts
    
    def _scan_jupiter_api_keys(self) -> List[SecretStr]:
        """Dynamically scan for JUPITER_API_KEY_* from model fields + os.environ fallback."""
        keys = []
        jupiter_keys = {}
        seen_indices: set = set()

        # 1. Scan explicitly defined model fields (1-3 + MEME1-3)
        field_names = ['JUPITER_API_KEY_1', 'JUPITER_API_KEY_2', 'JUPITER_API_KEY_3',
                       'JUPITER_API_KEY_MEME1', 'JUPITER_API_KEY_MEME2', 'JUPITER_API_KEY_MEME3']
        for name in field_names:
            val = getattr(self, name, None)
            if val is not None:
                secret_val = val.get_secret_value() if hasattr(val, 'get_secret_value') else val
                if secret_val and isinstance(secret_val, str) and secret_val.strip():
                    jupiter_keys[name] = val

        # 2. Also scan os.environ for additional keys (future expansion)
        for key, value in os.environ.items():
            match = re.match(r'JUPITER_API_KEY_(\d+)', key)
            if match and value:
                index = int(match.group(1))
                field_name = f'JUPITER_API_KEY_{index}'
                if field_name not in jupiter_keys:
                    jupiter_keys[field_name] = SecretStr(value)

        # Return sorted by field name
        for name in sorted(jupiter_keys.keys()):
            keys.append(jupiter_keys[name])

        return keys
    
    def _scan_ankr_api_keys(self) -> List[SecretStr]:
        """Dynamically scan for ANKR_API_KEY_* from model fields + os.environ fallback."""
        keys = []
        ankr_keys = {}
        seen_indices: set = set()

        # 1. Scan explicitly defined model fields (1-2)
        for i in range(1, 3):
            val = getattr(self, f'ANKR_API_KEY_{i}', None)
            if val is not None:
                secret_val = val.get_secret_value() if hasattr(val, 'get_secret_value') else val
                if secret_val and isinstance(secret_val, str) and secret_val.strip():
                    ankr_keys[i] = val
                    seen_indices.add(i)

        # 2. Also scan os.environ for additional keys (future expansion)
        for key, value in os.environ.items():
            match = re.match(r'ANKR_API_KEY_(\d+)', key)
            if match and value:
                index = int(match.group(1))
                if index not in seen_indices:
                    ankr_keys[index] = SecretStr(value)
                    seen_indices.add(index)

        # Return sorted by index
        for index in sorted(ankr_keys.keys()):
            keys.append(ankr_keys[index])

        return keys
    
    def get_gmgn_accounts(self) -> List[Dict[str, Any]]:
        """Return dynamically scanned GMGN accounts (with validation info)."""
        if not hasattr(self, '_gmgn_accounts'):
            self._gmgn_accounts = self._scan_gmgn_accounts()
        return self._gmgn_accounts
    
    def get_gmgn_api_keys(self) -> List[SecretStr]:
        """Return list of all available GMGN API keys (including 1-12+)."""
        accounts = self.get_gmgn_accounts()
        return [acc['api_key'] for acc in accounts if 'api_key' in acc]
    
    def get_jupiter_api_keys(self) -> List[SecretStr]:
        """Return dynamically scanned Jupiter API keys."""
        if not hasattr(self, '_jupiter_api_keys'):
            self._jupiter_api_keys = self._scan_jupiter_api_keys()
        return self._jupiter_api_keys
    
    def get_ankr_api_keys(self) -> List[SecretStr]:
        """Return dynamically scanned Ankr API keys."""
        if not hasattr(self, '_ankr_api_keys'):
            self._ankr_api_keys = self._scan_ankr_api_keys()
        return self._ankr_api_keys
    
    def get_gmgn_api_key(self) -> Optional[SecretStr]:
        """API key rotation: return first available GMGN API key (backward compatible)."""
        keys = self.get_gmgn_api_keys()
        return keys[0] if keys else None

    # Jupiter API Configuration
    JUPITER_API_BASE_URL: Optional[str] = None
    JUPITER_API_KEY_1: Optional[SecretStr] = None
    JUPITER_API_KEY_2: Optional[SecretStr] = None
    JUPITER_API_KEY_3: Optional[SecretStr] = None
    # Legacy keys (kept for compatibility)
    JUPITER_API_KEY_MEME1: Optional[SecretStr] = None
    JUPITER_API_KEY_MEME2: Optional[SecretStr] = None
    JUPITER_API_KEY_MEME3: Optional[SecretStr] = None
    
    def get_jupiter_api_key(self) -> Optional[SecretStr]:
        """API key rotation: return first available Jupiter API key (backward compatible)."""
        keys = self.get_jupiter_api_keys()
        if keys:
            return keys[0]
        # Fallback to legacy keys for backward compatibility
        return (self.JUPITER_API_KEY_1 or self.JUPITER_API_KEY_2 or 
                self.JUPITER_API_KEY_3 or self.JUPITER_API_KEY_MEME1 or
                self.JUPITER_API_KEY_MEME2 or self.JUPITER_API_KEY_MEME3)

    # Solana RPC Configuration
    SOLANA_RPC_HTTP_PRIMARY: Optional[str] = None
    SOLANA_RPC_WS_PRIMARY: Optional[str] = None
    SOLANA_RPC_HTTP_BACKUP_1: Optional[str] = None
    SOLANA_RPC_WS_BACKUP_1: Optional[str] = None
    ALCHEMY_API_KEYS: Optional[str] = None
    ANKR_API_KEY_1: Optional[SecretStr] = None
    ANKR_API_KEY_2: Optional[SecretStr] = None
    
    def _get_ankr_http_url(self, api_key: Optional[SecretStr]) -> Optional[str]:
        """Generate ANKR Solana HTTP RPC URL from API key."""
        if not api_key:
            return None
        return f"https://rpc.ankr.com/solana/{api_key.get_secret_value()}"
    
    def _get_ankr_ws_url(self, api_key: Optional[SecretStr]) -> Optional[str]:
        """Generate ANKR Solana WebSocket RPC URL from API key."""
        if not api_key:
            return None
        return f"wss://rpc.ankr.com/solana/ws/{api_key.get_secret_value()}"
    
    def get_rpc_http_url(self) -> Optional[str]:
        """
        Get HTTP RPC URL with support for:
        1. Direct URL: https://...
        2. Special value 'ankr': use ANKR_API_KEY to construct URL
        3. Fallback to ANKR_API_KEY or backup RPC
        """
        # If PRIMARY is "ankr", construct from ANKR_API_KEY
        if self.SOLANA_RPC_HTTP_PRIMARY == "ankr":
            ankr_keys = self.get_ankr_api_keys()
            if ankr_keys:
                return self._get_ankr_http_url(ankr_keys[0])
            # Fallback to hardcoded _1/_2 if env scan failed
            ankr_url = (self._get_ankr_http_url(self.ANKR_API_KEY_1) or 
                       self._get_ankr_http_url(self.ANKR_API_KEY_2))
            if ankr_url:
                return ankr_url
        
        # Otherwise use PRIMARY or BACKUP_1
        if self.SOLANA_RPC_HTTP_PRIMARY and self.SOLANA_RPC_HTTP_PRIMARY != "ankr":
            return self.SOLANA_RPC_HTTP_PRIMARY
        if self.SOLANA_RPC_HTTP_BACKUP_1:
            return self.SOLANA_RPC_HTTP_BACKUP_1
        
        # Fallback: construct from ANKR_API_KEY
        ankr_keys = self.get_ankr_api_keys()
        if ankr_keys:
            return self._get_ankr_http_url(ankr_keys[0])
        return (self._get_ankr_http_url(self.ANKR_API_KEY_1) or 
               self._get_ankr_http_url(self.ANKR_API_KEY_2))
    
    def get_rpc_ws_url(self) -> Optional[str]:
        """
        Get WebSocket RPC URL with support for:
        1. Direct URL: wss://...
        2. Fallback to ANKR_API_KEY for WebSocket construction
        Returns None if WSS not available (allowed in dry-run mode)
        """
        # If PRIMARY is configured and not empty
        if self.SOLANA_RPC_WS_PRIMARY:
            return self.SOLANA_RPC_WS_PRIMARY
        if self.SOLANA_RPC_WS_BACKUP_1:
            return self.SOLANA_RPC_WS_BACKUP_1
        
        # Fallback: construct from ANKR_API_KEY (optional)
        # Note: Return None if not available, WSS is optional for dry-run
        return None
    
    def get_rpc_url(self) -> Optional[str]:
        """Legacy method for backward compatibility. Use get_rpc_http_url() instead."""
        return self.get_rpc_http_url()

    # Jito Configuration
    JITO_ENABLED: bool = Field(True)
    JITO_BLOCK_ENGINE_URL: str = Field('https://mainnet.block-engine.jito.wtf')
    JITO_TIP_FLOOR_URL: str = Field('https://bundles.jito.wtf/api/v1/bundles/tip_floor')
    JITO_TIP_STREAM_WS: str = Field('wss://bundles.jito.wtf/api/v1/bundles/tip_stream')

    # Trading Parameters (optional - defaults in code)
    POLL_INTERVAL_SECONDS: int = Field(60)
    ACTIVE_POSITION_PRICE_POLL_SECONDS: int = Field(1)
    TIP_FLOOR_REFRESH_SECONDS: int = Field(3)
    
    BUY_SLIPPAGE_CAP_BPS: int = Field(1500)
    SELL_SLIPPAGE_CAP_BPS: int = Field(2000)
    EMERGENCY_SLIPPAGE_CAP_BPS: int = Field(3500)
    PRICE_IMPACT_HARD_CAP_PCT: float = Field(10.0)
    
    LIVE_ROLLING_10_LOSS_LIMIT: float = Field(-0.20)
    MAX_REQUOTE_RETRY: int = Field(2)
    
    # Risk Feature Scan Tiers (dynamic based on remaining position value in SOL)
    RISK_FEATURE_SCAN_TIER_1_SOL: float = Field(1.5)
    RISK_FEATURE_SCAN_TIER_1_SECONDS: int = Field(2)
    RISK_FEATURE_SCAN_TIER_2_SOL: float = Field(1.0)
    RISK_FEATURE_SCAN_TIER_2_SECONDS: int = Field(4)
    RISK_FEATURE_SCAN_TIER_3_SOL: float = Field(0.5)
    RISK_FEATURE_SCAN_TIER_3_SECONDS: int = Field(8)
    RISK_FEATURE_SCAN_TIER_4_SOL: float = Field(0.25)
    RISK_FEATURE_SCAN_TIER_4_SECONDS: int = Field(16)
    RISK_FEATURE_SCAN_TIER_5_SECONDS: int = Field(32)
    
    # Dust Position Rules (in SOL, not USD)
    DUST_FORCE_EXIT_SOL: float = Field(0.125)

    # Wallet Configuration (only used if LIVE_TRADING_ENABLED=true)
    WALLET_PUBLIC_KEY: Optional[str] = None
    WALLET_PRIVATE_KEY_BASE58: Optional[SecretStr] = None

    def get_risk_scan_interval_seconds(self, remaining_value_sol: float) -> int:
        """
        Calculate risk feature scan interval based on remaining position value in SOL.
        
        Returns scan interval in seconds:
        - remaining_value_sol >= 1.5: 2s
        - remaining_value_sol >= 1.0: 4s
        - remaining_value_sol >= 0.5: 8s
        - remaining_value_sol >= 0.25: 16s
        - remaining_value_sol < 0.25: 32s
        """
        if remaining_value_sol >= self.RISK_FEATURE_SCAN_TIER_1_SOL:
            return self.RISK_FEATURE_SCAN_TIER_1_SECONDS
        elif remaining_value_sol >= self.RISK_FEATURE_SCAN_TIER_2_SOL:
            return self.RISK_FEATURE_SCAN_TIER_2_SECONDS
        elif remaining_value_sol >= self.RISK_FEATURE_SCAN_TIER_3_SOL:
            return self.RISK_FEATURE_SCAN_TIER_3_SECONDS
        elif remaining_value_sol >= self.RISK_FEATURE_SCAN_TIER_4_SOL:
            return self.RISK_FEATURE_SCAN_TIER_4_SECONDS
        else:
            return self.RISK_FEATURE_SCAN_TIER_5_SECONDS

    def get_provider_mode(self) -> ProviderMode:
        """Get effective provider mode, considering DRY_RUN compatibility."""
        if self.PROVIDER_MODE is not None:
            return self.PROVIDER_MODE
        
        # Compatibility with DRY_RUN
        if self.DRY_RUN and not self.LIVE_TRADING_ENABLED:
            return ProviderMode.MOCK
        elif not self.DRY_RUN and self.LIVE_TRADING_ENABLED:
            return ProviderMode.LIVE
        elif not self.DRY_RUN and not self.LIVE_TRADING_ENABLED:
            return ProviderMode.ONLINE_READONLY
        else:
            # DRY_RUN=true + LIVE_TRADING_ENABLED=true should not happen (caught in validation)
            return ProviderMode.MOCK

    def mask_key(self, s: Optional[SecretStr]) -> Optional[str]:
        if s is None:
            return None
        val = s.get_secret_value()
        if len(val) <= 8:
            return '****'
        return val[:4] + '...' + val[-4:]

    @model_validator(mode='after')
    def validate_live_config(self):
        # Get effective provider mode
        mode = self.get_provider_mode()

        # CRITICAL: DRY_RUN must be false if LIVE_TRADING_ENABLED is true
        if self.LIVE_TRADING_ENABLED and self.DRY_RUN:
            raise ValueError(
                'LIVE_TRADING_ENABLED=true requires DRY_RUN=false. '
                'Set DRY_RUN=false ONLY when you intend to execute real transactions.'
            )
        
        # Validate based on provider mode
        if mode == ProviderMode.LIVE:
            # LIVE mode requires LIVE_TRADING_ENABLED=true
            if not self.LIVE_TRADING_ENABLED:
                raise ValueError(
                    'PROVIDER_MODE=live requires LIVE_TRADING_ENABLED=true'
                )
            
            # LIVE mode requires all configurations
            missing = []
            if not self.GMGN_API_BASE_URL:
                missing.append('GMGN_API_BASE_URL')
            if not self.get_gmgn_api_key():
                missing.append('GMGN_API_KEY_1')
            if not self.JUPITER_API_BASE_URL:
                missing.append('JUPITER_API_BASE_URL')
            if not self.get_jupiter_api_key():
                missing.append('JUPITER_API_KEY_1')
            if not (self.SOLANA_RPC_HTTP_PRIMARY or self.ANKR_API_KEY_1):
                missing.append('SOLANA_RPC_HTTP_PRIMARY or ANKR_API_KEY_1')
            if not self.JITO_ENABLED:
                missing.append('JITO_ENABLED')
            if not self.JITO_BLOCK_ENGINE_URL:
                missing.append('JITO_BLOCK_ENGINE_URL')
            if not self.WALLET_PUBLIC_KEY:
                missing.append('WALLET_PUBLIC_KEY')
            if not self.WALLET_PRIVATE_KEY_BASE58:
                missing.append('WALLET_PRIVATE_KEY_BASE58')
            if missing:
                raise ValueError(f'PROVIDER_MODE=live requires: {missing}')
        
        elif mode == ProviderMode.ONLINE_READONLY:
            # ONLINE_READONLY doesn't require private key
            # But if private key is set, warn user
            if self.WALLET_PRIVATE_KEY_BASE58:
                import warnings
                warnings.warn(
                    'WALLET_PRIVATE_KEY_BASE58 is set but PROVIDER_MODE=online_readonly. '
                    'Private key will NOT be used in online_readonly mode.'
                )
        
        # Warn if private key is set but not in live mode
        if self.WALLET_PRIVATE_KEY_BASE58 and not self.LIVE_TRADING_ENABLED:
            import warnings
            warnings.warn(
                'WALLET_PRIVATE_KEY_BASE58 is set but LIVE_TRADING_ENABLED=false. '
                'Private key will NOT be used in simulation mode.'
            )
        
        return self


settings = Settings()
