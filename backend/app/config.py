from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr, model_validator
from typing import Optional

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
    GMGN_TRENCHES_PATH: str = Field('/api/v1/trenches')
    GMGN_TOKEN_PRICE_PATH: str = Field('/api/v1/token/price')
    GMGN_KLINE_PATH: str = Field('/api/v1/token/kline')
    
    def get_gmgn_api_key(self) -> Optional[SecretStr]:
        """API key rotation: return first available GMGN API key."""
        return self.GMGN_API_KEY_1 or self.GMGN_API_KEY_2 or self.GMGN_API_KEY_3

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
        """API key rotation: return first available Jupiter API key."""
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

    # Wallet Configuration (only used if LIVE_TRADING_ENABLED=true)
    WALLET_PUBLIC_KEY: Optional[str] = None
    WALLET_PRIVATE_KEY_BASE58: Optional[SecretStr] = None

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
