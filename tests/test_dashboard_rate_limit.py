"""Unit tests for dashboard rate limiter + /health endpoint."""

import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient

from dashboard import server as dash


# ── token bucket ──────────────────────────────────────────────────────────────

def test_token_bucket_allows_up_to_capacity():
    b = dash._TokenBucket(capacity=3, rate=0.0)
    assert b.allow()
    assert b.allow()
    assert b.allow()
    assert not b.allow()


def test_token_bucket_refills_over_time(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(dash.time, "monotonic", lambda: t[0])
    b = dash._TokenBucket(capacity=2, rate=1.0)   # 1 token / sec
    assert b.allow()
    assert b.allow()
    assert not b.allow()
    t[0] += 1.0
    assert b.allow()                              # one token refilled
    assert not b.allow()


def test_rate_limiter_isolates_scopes(monkeypatch):
    rl = dash._RateLimiter()
    # Drain the /login bucket completely (capacity 10).
    for _ in range(10):
        assert rl.check("1.2.3.4", "login")
    assert not rl.check("1.2.3.4", "login")
    # /api scope has its own bucket for the same IP.
    assert rl.check("1.2.3.4", "api")


def test_rate_limiter_isolates_ips():
    rl = dash._RateLimiter()
    for _ in range(10):
        assert rl.check("1.1.1.1", "login")
    assert not rl.check("1.1.1.1", "login")
    assert rl.check("2.2.2.2", "login")


# ── /health + middleware wiring ───────────────────────────────────────────────

@pytest.fixture
def client():
    server = dash.DashboardServer()
    return TestClient(server.app)


def test_health_endpoint_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert isinstance(data["uptime_s"], (int, float))
    assert data["clients"] == 0
    assert data["tokens"] == 0


def test_login_endpoint_rate_limited(client):
    # /login policy is capacity 10 → 11th request within ~1s should be 429.
    hits = [client.post("/login", json={"pin": "BAD"}).status_code for _ in range(12)]
    assert 429 in hits
    # First 10 should have been 401 (invalid pin), not 429.
    assert hits[:10].count(401) == 10


def test_api_command_rejects_unauthenticated(client):
    # Rate limit should not short-circuit the auth path for the first few calls.
    r = client.post("/api/command", json={"text": "hi"})
    assert r.status_code == 401


def test_health_endpoint_not_rate_limited(client):
    # /health is outside /api/*; should never hit the limiter.
    for _ in range(50):
        assert client.get("/health").status_code == 200
