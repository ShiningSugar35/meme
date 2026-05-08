from fastapi.testclient import TestClient
from ..main import app


def test_health_endpoint():
    with TestClient(app) as client:
        r = client.get('/health')
        assert r.status_code == 200
        assert r.json().get('status') == 'ok'
