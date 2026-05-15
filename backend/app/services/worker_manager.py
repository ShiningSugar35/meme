import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict

from ..logging_config import logger


class WorkerManager:
    def __init__(self, repo, event_bus=None):
        self.repo = repo
        self.event_bus = event_bus
        self._workers: Dict[str, dict] = {}
        self._running: Dict[str, bool] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    def register_worker(self, name: str, target: Callable[[], Awaitable[None]], interval_seconds: float):
        self._workers[name] = {
            'name': name,
            'target': target,
            'interval_seconds': max(float(interval_seconds), 0.1),
            'last_run_at': None,
            'last_error': None,
            'processed_count': 0,
        }

    def update_interval(self, name: str, interval_seconds: float):
        if name not in self._workers:
            return False
        self._workers[name]['interval_seconds'] = max(float(interval_seconds), 0.1)
        return True

    async def start_worker(self, name: str):
        if name not in self._workers:
            raise ValueError(f"Worker '{name}' is not registered")
        if self._running.get(name):
            return

        self._running[name] = True
        worker = self._workers[name]
        self._tasks[name] = asyncio.create_task(self._run_loop(name))

        await self.repo.append_system_event(
            'INFO', 'WORKER', f'Worker started: {name}',
            str({'name': name, 'interval_seconds': worker['interval_seconds']})
        )
        if self.event_bus:
            await self.event_bus.publish('system', {
                'level': 'INFO', 'category': 'WORKER',
                'message': f'Worker started: {name}'
            })

    async def stop_worker(self, name: str):
        if name not in self._workers:
            return
        if not self._running.get(name):
            return

        self._running[name] = False
        task = self._tasks.get(name)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self.repo.append_system_event(
            'INFO', 'WORKER', f'Worker stopped: {name}',
            str({'name': name, 'processed_count': self._workers[name]['processed_count']})
        )
        if self.event_bus:
            await self.event_bus.publish('system', {
                'level': 'INFO', 'category': 'WORKER',
                'message': f'Worker stopped: {name}'
            })

    async def start_all(self):
        for name in self._workers:
            await self.start_worker(name)

    async def stop_all(self):
        for name in list(self._running.keys()):
            if self._running.get(name):
                await self.stop_worker(name)

    def get_status(self) -> Dict[str, dict]:
        result = {}
        for name, worker in self._workers.items():
            result[name] = {
                'name': name,
                'running': self._running.get(name, False),
                'interval_seconds': worker['interval_seconds'],
                'last_run_at': worker['last_run_at'],
                'last_error': worker['last_error'],
                'processed_count': worker['processed_count'],
            }
        return result

    async def _run_loop(self, name: str):
        worker = self._workers[name]
        target = worker['target']

        while self._running.get(name):
            try:
                await target()
                worker['last_run_at'] = datetime.now(timezone.utc)
                worker['processed_count'] += 1
            except asyncio.CancelledError:
                logger.info(f"Worker '{name}' received cancellation, shutting down gracefully")
                break
            except Exception as e:
                worker['last_error'] = str(e)
                logger.error(f"Worker '{name}' error: {e}")
                try:
                    await self.repo.append_system_event(
                        'ERROR', 'WORKER', f'Worker error: {name}',
                        str({'name': name, 'error': str(e)})
                    )
                except Exception:
                    pass
                if self.event_bus:
                    try:
                        await self.event_bus.publish('system', {
                            'level': 'ERROR', 'category': 'WORKER',
                            'message': f'Worker error: {name} - {e}'
                        })
                    except Exception:
                        pass

            # Read the interval every loop so Control Center parameter changes take
            # effect without restarting the backend process.
            await asyncio.sleep(max(float(worker.get('interval_seconds', 1)), 0.1))
