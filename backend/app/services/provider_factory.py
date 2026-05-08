from ..db.repositories import Repositories
from ..config import settings, ProviderMode
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcProvider
from ..providers.mock_data import MockData


class ProviderContainer:
    def __init__(self, repo: Repositories):
        self.repo = repo
        self.mode = settings.get_provider_mode()
        
        # Create providers based on mode
        if self.mode == ProviderMode.MOCK:
            # MOCK: Use mock data and mock providers
            self.mock_data = MockData()
            # For mock mode, we still use real providers but they'll use mock data internally
            self.gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
            self.jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
            self.jito = JitoProvider(repo, mode=ProviderMode.MOCK)
            self.rpc = RpcProvider(repo, mode=ProviderMode.MOCK)
        else:
            # ONLINE_READONLY or LIVE: Use real providers with appropriate mode
            self.gmgn = GMGNProvider(repo, mode=self.mode)
            self.jupiter = JupiterProvider(repo, mode=self.mode)
            self.jito = JitoProvider(repo, mode=self.mode)
            self.rpc = RpcProvider(repo, mode=self.mode)
        
        # Keep mock_data accessible for tests
        if not hasattr(self, 'mock_data'):
            self.mock_data = MockData()


def create_providers(repo: Repositories) -> ProviderContainer:
    return ProviderContainer(repo)
