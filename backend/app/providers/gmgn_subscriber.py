from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Callable, Awaitable
from dataclasses import dataclass
import asyncio
from ..config import settings, ProviderMode
from ..logging_config import logger


@dataclass
class SubscribedTick:
    token_mint: str
    price_usd: float
    price_sol: float
    liquidity_usd: float
    sol_side_liquidity: float
    market_cap: float
    observed_at: str


class GMGNSubscriberBase(ABC):
    @abstractmethod
    async def subscribe(self, token_mint: str) -> None:
        ...

    @abstractmethod
    async def unsubscribe(self, token_mint: str) -> None:
        ...

    @abstractmethod
    async def get_latest(self, token_mint: str) -> Optional[SubscribedTick]:
        ...

    @abstractmethod
    async def get_latest_batch(self, token_mints: List[str]) -> Dict[str, Optional[SubscribedTick]]:
        ...


class GMGNMockSubscriber(GMGNSubscriberBase):
    def __init__(self):
        self._subscriptions: Dict[str, SubscribedTick] = {}
        self._callbacks: List[Callable[[SubscribedTick], Awaitable[None]]] = []

    async def subscribe(self, token_mint: str) -> None:
        self._subscriptions[token_mint] = SubscribedTick(
            token_mint=token_mint,
            price_usd=0.0001,
            price_sol=0.000001,
            liquidity_usd=10000,
            sol_side_liquidity=50,
            market_cap=50000,
            observed_at='2026-01-01T00:00:00Z'
        )

    async def unsubscribe(self, token_mint: str) -> None:
        self._subscriptions.pop(token_mint, None)

    async def get_latest(self, token_mint: str) -> Optional[SubscribedTick]:
        return self._subscriptions.get(token_mint)

    async def get_latest_batch(self, token_mints: List[str]) -> Dict[str, Optional[SubscribedTick]]:
        return {m: self._subscriptions.get(m) for m in token_mints}

    def inject_tick(self, tick: SubscribedTick) -> None:
        self._subscriptions[tick.token_mint] = tick

    async def on_tick(self, callback: Callable[[SubscribedTick], Awaitable[None]]) -> None:
        self._callbacks.append(callback)


def create_gmgn_subscriber() -> GMGNSubscriberBase:
    mode = settings.get_provider_mode()
    if mode == ProviderMode.MOCK:
        return GMGNMockSubscriber()
    else:
        logger.warning("GMGN WebSocket subscription not yet implemented, using mock subscriber")
        return GMGNMockSubscriber()
