from ..db.repositories import Repositories
from ..config import settings


class KillSwitchRunner:
    def __init__(self, repo: Repositories):
        self.repo = repo

    async def run_once(self):
        closed = await self.repo.list_recent_closed_live_positions(10)
        if len(closed) < 10:
            return
        total_cost = sum([c.get('total_cost_usd', 0) or 0 for c in closed])
        total_return = sum([c.get('total_return_usd', 0) or 0 for c in closed])
        if total_cost == 0:
            await self.repo.append_system_event('WARN', 'KILL_SWITCH', 'insufficient total_cost',
                str({'total_cost': total_cost}), account_type='SIM')
            return
        rolling_10_roi = total_return / total_cost - 1
        if rolling_10_roi <= settings.LIVE_ROLLING_10_LOSS_LIMIT:
            await self.repo.set_runtime_setting('live_entries_enabled', 'false', 'kill_switch')
            await self.repo.append_system_event('WARN', 'KILL_SWITCH',
                'rolling 10 loss limit triggered',
                str({'rolling_10_roi': rolling_10_roi}), account_type='SIM')
