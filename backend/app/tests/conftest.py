import asyncio
import pytest
import pytest_asyncio
from pathlib import Path

from ..db.repositories import Repositories
from ..config import ProviderMode
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider
from ..trading.executor import TradingPipeline


@pytest_asyncio.fixture
async def repo(tmp_path):
    db_file = tmp_path / "test.sqlite3"
    repo = await Repositories.create(str(db_file))
    # ensure defaults
    await repo.ensure_default_strategy_groups()
    try:
        yield repo
    finally:
        # close DB connection to avoid unclosed thread warnings
        try:
            await repo.close()
        except Exception:
            # allow cleanup to proceed
            pass


@pytest.fixture
def pipeline_factory(repo):
    def _make(scenario: str = 'success'):
        # Create providers in MOCK mode for testing
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jupiter._test_scenario = scenario  # set test scenario for mock
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)
        # Return pipeline and a mock object (for compatibility with old tests)
        from ..providers.mock_data import MockData
        mock = MockData()
        return pipeline, mock

    return _make
