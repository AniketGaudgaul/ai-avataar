"""Smoke tests for the API surface. These double as the seed for the DeepEval
CI gate (spec 10, Phase C)."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_root():
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "ai-avatar"


def test_chat_stub():
    resp = client.post("/chat", json={"query": "What companies has he worked at?"})
    assert resp.status_code == 200
    assert "answer" in resp.json()


def test_chat_rejects_empty_query():
    resp = client.post("/chat", json={"query": ""})
    assert resp.status_code == 422
