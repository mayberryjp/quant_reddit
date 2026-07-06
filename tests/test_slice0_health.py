"""Slice 0: liveness health endpoint and JSON error envelopes."""

from __future__ import annotations


class TestHealth:
    def test_health_ok(self, app_client):
        r = app_client.get("/reddit/health")
        assert r.status_int == 200
        assert r.json["status"] == "ok"


class TestErrorEnvelopes:
    def test_unknown_route_returns_json_404(self, app_client):
        r = app_client.get("/does-not-exist", expect_errors=True)
        assert r.status_int == 404
        assert r.json["detail"] == "not found"
