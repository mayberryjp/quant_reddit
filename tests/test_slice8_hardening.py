"""Slice 8: validation hardening, JSON error envelopes, config validation."""

from __future__ import annotations

from app.config import Settings, validate_config


class TestReadValidation:
    def test_invalid_kind_422(self, app_client):
        r = app_client.get("/reddit/items/recent", {"kind": "video"}, expect_errors=True)
        assert r.status_int == 422
        assert "detail" in r.json

    def test_invalid_process_state_422(self, app_client):
        r = app_client.get(
            "/reddit/items/recent", {"process_state": "bogus"}, expect_errors=True
        )
        assert r.status_int == 422

    def test_valid_kind_ok(self, app_client):
        r = app_client.get("/reddit/items/recent", {"kind": "post"})
        assert r.status_int == 200

    def test_invalid_target_422(self, app_client):
        r = app_client.get(
            "/reddit/emissions/recent", {"target": "email"}, expect_errors=True
        )
        assert r.status_int == 422

    def test_invalid_status_422(self, app_client):
        r = app_client.get(
            "/reddit/emissions/recent", {"status": "maybe"}, expect_errors=True
        )
        assert r.status_int == 422

    def test_invalid_page_size_422(self, app_client):
        r = app_client.get(
            "/reddit/extractions/recent", {"page_size": "lots"}, expect_errors=True
        )
        assert r.status_int == 422


class TestErrorEnvelopes:
    def test_unknown_route_json_404(self, app_client):
        r = app_client.get("/nope", expect_errors=True)
        assert r.status_int == 404
        assert r.json["detail"] == "not found"

    def test_method_not_allowed_json_405(self, app_client):
        r = app_client.post_json("/reddit/health", {}, expect_errors=True)
        assert r.status_int == 405
        assert r.json["detail"] == "method not allowed"


class TestConfigValidation:
    def test_missing_required_reported(self, monkeypatch):
        monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
        problems = validate_config(Settings(), database_url=None)
        assert any("DATABASE_URL" in p for p in problems)
        assert any("REDDIT_CLIENT_ID" in p for p in problems)

    def test_valid_config_has_no_problems(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        problems = validate_config(Settings(), database_url="postgresql+psycopg://x/y")
        assert problems == []

    def test_non_http_downstream_url_reported(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        monkeypatch.setenv("QUANT_SIGNALS_URL", "ftp://not-http")
        problems = validate_config(Settings(), database_url="postgresql+psycopg://x/y")
        assert any("QUANT_SIGNALS_URL" in p for p in problems)

    def test_secrets_not_leaked_in_messages(self, monkeypatch):
        # A secret value must never appear in a validation message.
        monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "super-secret-value")
        monkeypatch.setenv("QUANT_SENTIMENT_URL", "ftp://bad")
        problems = validate_config(Settings(), database_url="db")
        assert all("super-secret-value" not in p for p in problems)
