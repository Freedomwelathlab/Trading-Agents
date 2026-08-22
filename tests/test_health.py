from fastapi.testclient import TestClient

from apps.api.app.main import app


def test_health_reports_research_mode_by_default():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["trading_mode"] == "research"
    assert body["live_trading_enabled"] is False
