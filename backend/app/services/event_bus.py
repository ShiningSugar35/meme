import asyncio
import json
from typing import Any, Dict, Set
from ..logging_config import logger


class EventBus:
    def __init__(self):
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, channel: str = 'system') -> asyncio.Queue:
        async with self._lock:
            if channel not in self._subscribers:
                self._subscribers[channel] = set()
            queue: asyncio.Queue = asyncio.Queue(maxsize=100)
            self._subscribers[channel].add(queue)
        return queue

    async def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            if channel in self._subscribers:
                self._subscribers[channel].discard(queue)

    async def publish(self, channel: str, event: Dict[str, Any]) -> None:
        async with self._lock:
            queues = self._subscribers.get(channel, set())
        dead = set()
        for q in queues:
            try:
                q.put_nowait(json.dumps(event))
            except asyncio.QueueFull:
                dead.add(q)
            except Exception:
                dead.add(q)
        if dead:
            async with self._lock:
                self._subscribers[channel] -= dead


event_bus = EventBus()
