from fastapi import APIRouter, Request
from ..runners.mock_lifecycle_runner import MockLifecycleRunner

router = APIRouter()


@router.post('/api/mock/run-once')
async def run_once(request: Request):
    app = request.app
    repo = app.state.repo
    providers = app.state.providers
    strategy_groups = await repo.list_strategy_groups()
    runner = MockLifecycleRunner(repo, providers, strategy_groups)
    await runner.run_once()
    return {'status': 'ok'}
