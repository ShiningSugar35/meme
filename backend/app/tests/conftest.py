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


def pytest_addoption(parser):
    parser.addoption("--run-smoke", action="store_true", default=False, help="run smoke tests against real APIs")


def pytest_configure(config):
    config.addinivalue_line("markers", "smoke: mark test as smoke test (requires --run-smoke)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-smoke"):
        return
    skip_smoke = pytest.mark.skip(reason="need --run-smoke option to run")
    for item in items:
        if "smoke" in item.keywords:
            item.add_marker(skip_smoke)


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
