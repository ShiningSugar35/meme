from .discovery_runner import DiscoveryRunner
from .second_filter_runner import SecondFilterRunner
from .price_monitor_runner import PriceMonitorRunner
from .position_risk_runner import PositionRiskRunner
from .kill_switch_runner import KillSwitchRunner
from ..db.repositories import Repositories
from ..services.provider_factory import ProviderContainer
from ..services.price_aggregator import PriceAggregator
from ..providers.gmgn_subscriber import create_gmgn_subscriber
from ..trading.executor import TradingPipeline


class MockLifecycleRunner:
    """Single-pass lifecycle runner used by mock/dev flows.

    The object mirrors main.py wiring: discovery -> second filter/entry -> price
    monitor -> position risk exits -> kill-switch housekeeping.  It deliberately
    passes the same provider set into TradingPipeline so this runner does not
    diverge from the app-scoped workers used in normal runtime mode.
    """

    def __init__(self, repo: Repositories, providers: ProviderContainer, strategy_groups: list):
        self.repo = repo
        self.providers = providers
        self.strategy_groups = strategy_groups

        subscriber = create_gmgn_subscriber()
        self.aggregator = PriceAggregator(repo, providers.gmgn, providers.jupiter, subscriber)
        self.trading_pipeline = TradingPipeline(repo, providers.gmgn, providers.jupiter, providers.jito, providers.rpc)

        self.discovery = DiscoveryRunner(repo, providers.gmgn, strategy_groups)
        self.second = SecondFilterRunner(
            repo,
            providers.gmgn,
            providers.jupiter,
            providers.jito,
            providers.rpc,
            strategy_groups,
        )
        self.price = PriceMonitorRunner(repo, self.aggregator)
        self.risk = PositionRiskRunner(repo, providers.gmgn, trading_pipeline=self.trading_pipeline)
        self.kill = KillSwitchRunner(repo)

    async def run_once(self):
        await self.discovery.run_once()
        await self.second.run_once()
        await self.price.run_once()
        await self.risk.run_once()
        await self.kill.run_once()
