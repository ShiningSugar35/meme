from .discovery_runner import DiscoveryRunner
from .second_filter_runner import SecondFilterRunner
from .price_monitor_runner import PriceMonitorRunner
from .position_risk_runner import PositionRiskRunner
from .kill_switch_runner import KillSwitchRunner
from ..db.repositories import Repositories
from ..services.provider_factory import ProviderContainer
from ..services.price_aggregator import PriceAggregator
from ..providers.gmgn_subscriber import create_gmgn_subscriber
from ..logging_config import logger


class MockLifecycleRunner:
    def __init__(self, repo: Repositories, providers: ProviderContainer, strategy_groups: list):
        self.repo = repo
        self.providers = providers
        self.strategy_groups = strategy_groups

        subscriber = create_gmgn_subscriber()
        self.aggregator = PriceAggregator(repo, providers.gmgn, providers.jupiter, subscriber)

        self.discovery = DiscoveryRunner(repo, providers.gmgn, strategy_groups)
        self.second = SecondFilterRunner(repo, providers.gmgn, providers.jupiter, providers.jito, providers.rpc, strategy_groups)
        self.price = PriceMonitorRunner(repo, self.aggregator)
        self.risk = PositionRiskRunner(repo, providers.gmgn)
        self.kill = KillSwitchRunner(repo)

    async def run_once(self):
        await self.discovery.run_once()
        await self.second.run_once()
        await self.price.run_once()
        await self.risk.run_once()
        await self.kill.run_once()
