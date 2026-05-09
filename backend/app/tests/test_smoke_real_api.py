"""
API Smoke Tests (skip by default, use --run-smoke to enable)

Usage: python -m pytest --run-smoke backend/app/tests/test_smoke_real_api.py -v
"""
import pytest
import json

pytestmark = pytest.mark.skip(reason="smoke tests require --run-smoke flag")


class TestGMGNSmoke:
    @pytest.mark.asyncio
    async def test_trenches_real(self):
        from ..config import settings, ProviderMode
        from ..db.repositories import Repositories
        from ..providers.gmgn_real import GMGNProvider
        repo = await Repositories.create(':memory:')
        try:
            gmgn = GMGNProvider(repo, mode=ProviderMode.ONLINE_READONLY)
            result = await gmgn.fetch_trenches({})
            assert isinstance(result, list), f"Expected list, got {type(result)}"
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_token_snapshot_real(self):
        from ..config import settings, ProviderMode
        from ..db.repositories import Repositories
        from ..providers.gmgn_real import GMGNProvider
        repo = await Repositories.create(':memory:')
        try:
            gmgn = GMGNProvider(repo, mode=ProviderMode.ONLINE_READONLY)
            result = await gmgn.fetch_token_snapshot("So11111111111111111111111111111111111111112")
            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_latest_price_real(self):
        from ..config import settings, ProviderMode
        from ..db.repositories import Repositories
        from ..providers.gmgn_real import GMGNProvider
        repo = await Repositories.create(':memory:')
        try:
            gmgn = GMGNProvider(repo, mode=ProviderMode.ONLINE_READONLY)
            result = await gmgn.fetch_latest_price("So11111111111111111111111111111111111111112")
            assert isinstance(result, dict)
        finally:
            await repo.close()


class TestJupiterSmoke:
    @pytest.mark.asyncio
    async def test_quote_real(self):
        from ..config import settings, ProviderMode
        from ..db.repositories import Repositories
        from ..providers.jupiter_real import JupiterProvider
        repo = await Repositories.create(':memory:')
        try:
            jup = JupiterProvider(repo, mode=ProviderMode.ONLINE_READONLY)
            result = await jup.quote_exact_in(
                "So11111111111111111111111111111111111111112",
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                1000000, 1500
            )
            assert isinstance(result, dict)
            if result.get('error') == 'HIGH_PRICE_IMPACT':
                pass
        finally:
            await repo.close()


class TestJitoSmoke:
    @pytest.mark.asyncio
    async def test_tip_floor_real(self):
        from ..config import settings, ProviderMode
        from ..db.repositories import Repositories
        from ..providers.jito_real import JitoProvider
        repo = await Repositories.create(':memory:')
        try:
            jito = JitoProvider(repo, mode=ProviderMode.ONLINE_READONLY)
            result = await jito.get_tip_floor()
            assert 'landed_tips_50th_percentile' in result
        finally:
            await repo.close()


class TestRPCSmoke:
    @pytest.mark.asyncio
    async def test_balance_real(self):
        from ..config import settings, ProviderMode
        from ..db.repositories import Repositories
        from ..providers.rpc_real import RpcRealProvider
        repo = await Repositories.create(':memory:')
        try:
            rpc = RpcRealProvider(repo, mode=ProviderMode.ONLINE_READONLY)
            result = await rpc.get_balance("11111111111111111111111111111111")
            assert 'sol_balance' in result
        finally:
            await repo.close()
