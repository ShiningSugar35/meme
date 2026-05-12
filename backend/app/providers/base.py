from abc import ABC, abstractmethod
from typing import Any, Dict, List


class MarketDataProvider(ABC):
    """Read-only market data provider interface.

    Internal normalized token snapshot fields intentionally match db/schema.sql:
    token_mint, pool_address, pool_created_at, type, liquidity_usd,
    sol_side_liquidity, volume_usd, market_cap, price_usd, price_sol,
    holder/security fields, and raw_json/source_mode.
    """

    @abstractmethod
    async def fetch_trenches(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    async def fetch_token_snapshot(self, token_mint: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    async def fetch_kline(self, token_mint: str, interval: str, limit: int) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    async def fetch_latest_price(self, token_mint: str) -> Dict[str, Any]:
        ...


class SwapProvider(ABC):
    """Swap quoting/building provider interface.

    quote_exact_in amount is always the raw integer amount before token decimals.
    For Jupiter, priceImpactPct is kept as Jupiter's fractional numeric string/float
    representation, and downstream code should convert to percentage only for storage/UI.
    """

    @abstractmethod
    async def quote_exact_in(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> Dict[str, Any]:
        ...

    @abstractmethod
    async def build_swap_instructions(self, quote: Dict[str, Any], user_public_key: str, extra: Dict[str, Any]) -> Dict[str, Any]:
        ...


class ExecutionProvider(ABC):
    @abstractmethod
    async def simulate(self, transaction_or_bundle: Any) -> Dict[str, Any]:
        ...

    @abstractmethod
    async def send(self, transaction_or_bundle: Any) -> Dict[str, Any]:
        ...


class RpcProvider(ABC):
    @abstractmethod
    async def get_balance(self, wallet: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    async def get_token_balance(self, wallet: str, mint: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    async def wait_signature(self, signature: str, timeout_seconds: int) -> Dict[str, Any]:
        ...
