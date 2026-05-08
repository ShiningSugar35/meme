from fastapi import APIRouter, Depends, HTTPException
from fastapi import Request
from ..db.repositories import Repositories
from ..runners.mock_lifecycle_runner import MockLifecycleRunner

router = APIRouter()


@router.post('/api/mock/run-once')
async def run_once(request: Request):
    app = request.app
    # ensure repo and providers exist (startup may not have run in some test contexts)
    if not hasattr(app.state, 'repo'):
        from ..db.repositories import Repositories
        from ..services.provider_factory import create_providers
        repo = await Repositories.create()
        await repo.ensure_default_strategy_groups()
        providers = create_providers(repo)
        app.state.repo = repo
        app.state.providers = providers
    else:
        repo = app.state.repo
        providers = app.state.providers

    strategy_groups = await repo.list_strategy_groups()
    runner = MockLifecycleRunner(repo, providers, strategy_groups)
    await runner.run_once()
    return {'status': 'ok'}
