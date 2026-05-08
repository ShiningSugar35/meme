from abc import ABC, abstractmethod
from typing import Any, Dict, List


class MarketDataProvider(ABC):
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
