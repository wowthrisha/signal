"""`/api/health` leaks nothing and is honest about degraded states.

A health endpoint is the most reliably unauthenticated surface in any
deployment — it is what a load balancer, an uptime checker and a curious
stranger all reach first. So the interesting tests are not "does it return 200"
but "what can someone learn from it", and they assert key names rather than
trusting the module docstring.
"""
from __future__ import annotations

import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api import health as health_mod
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

# Substrings that must never appear in a key anywhere in the payload.
FORBIDDEN_KEY_PARTS = ("password", "url", "secret", "token", "dsn", "host", "port")


def _keys(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(k)
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


def test_health_returns_live_counts(conn):
    r = client.get("/api/health")
    assert r.status_code in (200, 503)
    body = r.json()
    if body["status"] == "unavailable":
        pytest.skip("database unreachable in this environment")
    data = body["data"]
    assert data["session_count"] > 0
    assert data["instrument_count"] > 0
    assert data["latest_session_date"] is not None


def test_no_key_looks_like_a_credential_or_a_connection_string(conn):
    body = client.get("/api/health").json()
    offenders = [
        k for k in _keys(body)
        if any(part in k.lower() for part in FORBIDDEN_KEY_PARTS)
    ]
    assert not offenders, f"health payload exposes suspicious keys: {offenders}"


def test_no_value_contains_the_connection_string(conn):
    """Key names are not enough — a DSN pasted into a `reason` string would
    pass the key check and still leak the password."""
    raw = json.dumps(client.get("/api/health").json())
    assert "postgresql://" not in raw
    assert "signal:signal" not in raw


def test_no_stack_trace_is_returned(conn):
    raw = json.dumps(client.get("/api/health").json())
    for marker in ("Traceback", "File \"", "psycopg.", "line "):
        assert marker not in raw, f"health payload leaks internals: {marker!r}"


def test_the_latency_metric_defines_its_own_population(conn):
    """An undefined metric is decoration. The response must say which requests,
    over what window, and which statistic."""
    lat = client.get("/api/health").json()["digest_latency"]
    for field in ("population", "window", "statistic", "samples"):
        assert field in lat and lat[field] is not None, f"missing {field}"
    assert "digest" in lat["population"].lower()
    assert str(health_mod.RING_SIZE) in lat["window"]


def test_latency_is_empty_and_honest_before_any_request():
    """A process that has served nothing reports no samples rather than a zero
    that reads like a very fast service."""
    saved = list(health_mod._latencies)
    health_mod._latencies.clear()
    try:
        summary = health_mod.latency_summary()
        assert summary["samples"] == 0
        assert summary["median_ms"] is None
        assert summary["p95_ms"] is None
    finally:
        health_mod._latencies.extend(saved)


def test_only_successful_digests_are_timed():
    """A fast failure is not a fast response. If errors were recorded the
    metric would improve as the service degraded."""
    saved = list(health_mod._latencies)
    health_mod._latencies.clear()
    try:
        with pytest.raises(ValueError):
            with health_mod.record_digest_latency():
                raise ValueError("boom")
        assert len(health_mod._latencies) == 0
        with health_mod.record_digest_latency():
            pass
        assert len(health_mod._latencies) == 1
    finally:
        health_mod._latencies.clear()
        health_mod._latencies.extend(saved)


def test_the_ring_is_bounded():
    saved = list(health_mod._latencies)
    health_mod._latencies.clear()
    try:
        for _ in range(health_mod.RING_SIZE + 50):
            with health_mod.record_digest_latency():
                pass
        assert len(health_mod._latencies) == health_mod.RING_SIZE
    finally:
        health_mod._latencies.clear()
        health_mod._latencies.extend(saved)


def test_database_unavailable_returns_503_without_internals(monkeypatch):
    """Degraded, not crashed — and the reason is a fixed token, never the
    exception text, which can carry the DSN."""
    def boom():
        raise psycopg.OperationalError("could not connect to postgresql://u:p@h:5432/db")

    monkeypatch.setattr(health_mod, "connect", boom)
    r = client.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "database_unreachable"
    assert body["data"] is None
    raw = json.dumps(body)
    assert "postgresql://" not in raw and "u:p@h" not in raw


def test_an_empty_database_is_a_state_not_an_error(monkeypatch):
    """A fresh deploy holding no bars is up and truthfully reporting nothing,
    which is different from being broken."""
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchone(self): return (None, 0, 0, 0)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    monkeypatch.setattr(health_mod, "connect", lambda: _Conn())
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "empty"
    assert body["data"]["session_count"] == 0
    assert body["data"]["latest_session_date"] is None
