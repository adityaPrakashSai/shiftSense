import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "missing.db"))
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def test_health_ok_in_mock_mode(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["llm_mode"] == "mock"
    assert body["provider"] == "baseten"
    assert body["model"] == "meta-models/muse-spark-open-1026"
    assert body["db"] == "absent"


def test_unknown_api_route_is_404_not_spa(client: TestClient) -> None:
    assert client.get("/api/nope").status_code == 404


def test_client_route_serves_html(client: TestClient) -> None:
    r = client.get("/demo")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
