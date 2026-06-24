from .discovery_runner import DiscoveryRunner
from .price_monitor_runner import PriceMonitorRunner
from .position_risk_runner import PositionRiskRunner
from .kill_switch_runner import KillSwitchRunner
from .active_position_price_runner import ActivePositionPriceRunner
from .position_soft_stop_runner import PositionSoftStopRunner
from ..db.repositories import Repositories
from ..services.provider_factory import ProviderContainer
from ..config import ProviderMode, settings
from ..services.price_aggregator import PriceAggregator
from ..providers.gmgn_subscriber import create_gmgn_subscriber
from ..trading.executor import TradingPipeline


class MockLifecycleRunner:
    """Single-pass lifecycle runner used by mock/dev flows.

    Mirrors main.py wiring: discovery -> price_monitor -> active_position_price
    -> position_soft_stop -> position_risk -> kill_switch.
    """

    def __init__(self, repo: Repositories, providers: ProviderContainer, strategy_groups: list):
        self.repo = repo
        if getattr(providers, "mode", None) != ProviderMode.MOCK:
            providers = ProviderContainer(repo, mode=ProviderMode.MOCK)
        self.providers = providers
        self.strategy_groups = strategy_groups

        subscriber = create_gmgn_subscriber()
        self.aggregator = PriceAggregator(repo, providers.gmgn, providers.jupiter, subscriber)
        self.trading_pipeline = TradingPipeline(repo, providers.gmgn, providers.jupiter, providers.jito, providers.rpc)

        self.discovery = DiscoveryRunner(
            repo, providers.gmgn, strategy_groups,
            providers.jupiter, providers.jito, providers.rpc,
        )
        self.price = PriceMonitorRunner(repo, self.aggregator)
        self.active_price = ActivePositionPriceRunner(repo, providers.gmgn, trading_pipeline=self.trading_pipeline)
        self.soft_stop = PositionSoftStopRunner(repo, providers.gmgn, trading_pipeline=self.trading_pipeline)
        self.risk = PositionRiskRunner(repo, providers.gmgn, trading_pipeline=self.trading_pipeline)
        self.kill = KillSwitchRunner(repo)

    async def run_once(self):
        previous_mode = settings.PROVIDER_MODE
        settings.set_provider_mode(ProviderMode.MOCK)
        await self.repo.set_runtime_setting("user_mode", "SIM_TEST")
        try:
            await self.discovery.run_once()
            await self.price.run_once()
            await self.active_price.run_once()
            await self.soft_stop.run_once()
            await self.risk.run_once()
            await self.kill.run_once()
        finally:
            settings.set_provider_mode(previous_mode)
