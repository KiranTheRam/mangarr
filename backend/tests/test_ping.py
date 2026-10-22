import pytest
from fastapi.testclient import TestClient

from mangarr.api import deps
from mangarr.config import config
from mangarr.main import app


@pytest.fixture
def client(monkeypatch):
    # A fixed key, as with MANGARR_API_KEY, so nothing is written to data_dir.
    monkeypatch.setattr(config, "api_key", "configured-key")
    monkeypatch.setattr(deps, "_api_key", None)
    # No `with`: the lifespan (DB init, scheduler) must not be needed.
    return TestClient(app)


def test_ping_answers_without_an_api_key(client):
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "OK"}


def test_ping_ignores_a_wrong_api_key(client):
    resp = client.get("/ping", headers={"X-Api-Key": "not-the-key"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "OK"}


def test_ping_does_not_leak_the_api_key(client):
    assert "configured-key" not in client.get("/ping").text


def test_api_still_requires_the_key(client):
    # /ping is public; /api/v1 is not
    assert client.get("/api/v1/system/status").status_code == 401
