"""
Acceptance Test: API Contracts

Verifies that API endpoints meet their contract:
- Health endpoint returns correct schema
- Response status codes are correct
- Response format is consistent
"""
import pytest
from fastapi.testclient import TestClient
from ..main import app


client = TestClient(app)


@pytest.mark.asyncio
async def test_health_endpoint_contract():
    """
    Health endpoint should return a specific JSON schema
    """
    response = client.get("/health")
    assert response.status_code == 200
    
    data = response.json()
    assert isinstance(data, dict)
    
    # Required fields
    assert 'status' in data, "Health endpoint must have 'status' field"
    assert 'version' in data, "Health endpoint must have 'version' field"
    assert 'timestamp' in data, "Health endpoint must have 'timestamp' field"
    
    # Status should be 'ok' or 'healthy'
    assert data['status'] in ['ok', 'healthy'], f"Unexpected status: {data['status']}"
    
    # Version should be a string
    assert isinstance(data['version'], str), "Version must be a string"
    
    # Timestamp should be a string (ISO format)
    assert isinstance(data['timestamp'], str), "Timestamp must be a string"


@pytest.mark.asyncio
async def test_404_for_unknown_endpoint():
    """
    Unknown endpoints should return 404 with JSON error
    """
    response = client.get("/unknown_endpoint")
    assert response.status_code == 404
    
    data = response.json()
    assert isinstance(data, dict)


@pytest.mark.asyncio
async def test_api_response_content_type_json():
    """
    API responses should have Content-Type: application/json
    """
    response = client.get("/health")
    assert response.status_code == 200
    
    content_type = response.headers.get('content-type')
    assert 'application/json' in content_type, f"Expected JSON content type, got: {content_type}"


# Note: Additional API contract tests would be added here as more endpoints are implemented
# For example:
# - POST /api/providers/dry-run-check (when implemented)
# - GET /api/positions
# - GET /api/trades
# etc.
