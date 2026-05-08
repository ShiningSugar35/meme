import time
import json
from contextlib import asynccontextmanager
from ..logging_config import logger


@asynccontextmanager
async def provider_call_span(repo, provider_name: str, endpoint: str, request_summary: dict):
    start = time.time()
    try:
        yield
        latency_ms = int((time.time() - start) * 1000)
        # repos should be used by provider implementations to log provider_requests
        logger.debug(f"provider call {provider_name} {endpoint} succeeded", latency_ms=latency_ms)
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        logger.error(f"provider call {provider_name} {endpoint} failed", error=str(e), latency_ms=latency_ms)
        # write minimal system event if repo available
        try:
            await repo.append_system_event('ERROR', 'PROVIDER', f'{provider_name} {endpoint} failed', json.dumps({'error': str(e)}))
        except Exception:
            logger.exception('failed to append system event for provider error')
        raise
