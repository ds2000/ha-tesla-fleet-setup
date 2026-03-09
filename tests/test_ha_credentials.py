"""Tests for HA Application Credentials injection."""

from unittest.mock import AsyncMock, patch

import ha_credentials
import server


class TestInjectCredentials:
    async def test_fails_without_supervisor_token(self, monkeypatch):
        monkeypatch.setattr(ha_credentials, "SUPERVISOR_TOKEN", "")
        result = await ha_credentials.inject_credentials("id", "secret")
        assert result["success"] is False
        assert "SUPERVISOR_TOKEN" in result["error"]

    async def test_returns_error_on_ws_failure(self, monkeypatch):
        monkeypatch.setattr(ha_credentials, "SUPERVISOR_TOKEN", "fake-token")
        # Connection will fail since there's no real HA
        result = await ha_credentials.inject_credentials("id", "secret")
        assert result["success"] is False
        assert result["error"]


class TestInjectEndpoint:
    async def test_requires_credentials(self, client):
        resp = await client.post("/api/inject-credentials")
        assert resp.status == 400
        data = await resp.json()
        assert data["success"] is False

    async def test_injects_with_credentials(self, client, setup_complete_state):
        with patch("ha_credentials.inject_credentials", new_callable=AsyncMock,
                    return_value={"success": True, "already_exists": False}):
            resp = await client.post("/api/inject-credentials")
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            assert server.state["ha_credentials_injected"] is True

    async def test_already_exists(self, client, setup_complete_state):
        with patch("ha_credentials.inject_credentials", new_callable=AsyncMock,
                    return_value={"success": True, "already_exists": True}):
            resp = await client.post("/api/inject-credentials")
            data = await resp.json()
            assert data["success"] is True
            assert data["already_exists"] is True

    async def test_handles_failure(self, client, setup_complete_state):
        with patch("ha_credentials.inject_credentials", new_callable=AsyncMock,
                    return_value={"success": False, "error": "WS auth failed"}):
            resp = await client.post("/api/inject-credentials")
            data = await resp.json()
            assert data["success"] is False

    async def test_inject_requires_post(self, client):
        resp = await client.get("/api/inject-credentials")
        assert resp.status == 405


class TestStatusIncludesCredentialState:
    async def test_shows_not_injected(self, client):
        resp = await client.get("/api/status")
        data = await resp.json()
        assert data["ha_credentials_injected"] is False

    async def test_shows_injected(self, client, setup_complete_state):
        server.state["ha_credentials_injected"] = True
        resp = await client.get("/api/status")
        data = await resp.json()
        assert data["ha_credentials_injected"] is True
