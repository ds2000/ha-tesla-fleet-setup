"""Tests for the aiohttp server API endpoints."""

import json
from unittest.mock import AsyncMock, patch

import server


class TestWellKnown:
    async def test_serves_public_key(self, client):
        resp = await client.get("/.well-known/appspecific/com.tesla.3p.public-key.pem")
        assert resp.status == 200
        body = await resp.text()
        assert "BEGIN PUBLIC KEY" in body

    async def test_content_type_is_text(self, client):
        resp = await client.get("/.well-known/appspecific/com.tesla.3p.public-key.pem")
        assert "text/plain" in resp.content_type


class TestApiStatus:
    async def test_returns_status(self, client):
        resp = await client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["step"] == 1
        assert data["has_credentials"] is False
        assert data["has_tokens"] is False

    async def test_includes_proxy_status(self, client):
        resp = await client.get("/api/status")
        data = await resp.json()
        assert "proxy" in data
        assert "running" in data["proxy"]
        assert "available" in data["proxy"]

    async def test_never_exposes_secrets(self, client, setup_complete_state):
        resp = await client.get("/api/status")
        data = await resp.json()
        body = json.dumps(data)
        assert "test-client-secret" not in body
        assert "test-access-token" not in body
        assert "test-refresh-token" not in body


class TestGenerateKeys:
    async def test_generates_keys(self, client):
        with patch("ha_discovery.get_ha_info", new_callable=AsyncMock, return_value={
            "external_url": None, "has_nabu_casa": False, "has_external_url": False,
        }):
            resp = await client.post("/api/generate-keys")
            assert resp.status == 200
            data = await resp.json()
            assert "BEGIN PUBLIC KEY" in data["public_key"]

    async def test_advances_to_step_2(self, client):
        with patch("ha_discovery.get_ha_info", new_callable=AsyncMock, return_value={
            "external_url": None, "has_nabu_casa": False, "has_external_url": False,
        }):
            await client.post("/api/generate-keys")
            assert server.state["step"] >= 2


class TestSetUrl:
    async def test_set_valid_url(self, client):
        resp = await client.post("/api/set-url", json={"url": "https://example.com"})
        assert resp.status == 200
        assert server.state["public_key_url"] == "https://example.com"
        assert server.state["url_method"] == "manual"

    async def test_rejects_http_url(self, client):
        resp = await client.post("/api/set-url", json={"url": "http://insecure.com"})
        assert resp.status == 400

    async def test_rejects_empty_url(self, client):
        resp = await client.post("/api/set-url", json={"url": ""})
        assert resp.status == 400

    async def test_strips_trailing_slash(self, client):
        resp = await client.post("/api/set-url", json={"url": "https://example.com/"})
        assert resp.status == 200
        assert server.state["public_key_url"] == "https://example.com"


class TestSaveCredentials:
    async def test_saves_credentials(self, client):
        resp = await client.post("/api/save-credentials", json={
            "client_id": "test-id",
            "client_secret": "test-secret",
        })
        assert resp.status == 200
        assert server.state["client_id"] == "test-id"
        assert server.state["client_secret"] == "test-secret"
        assert server.state["step"] >= 4

    async def test_rejects_missing_id(self, client):
        resp = await client.post("/api/save-credentials", json={
            "client_id": "",
            "client_secret": "secret",
        })
        assert resp.status == 400

    async def test_rejects_missing_secret(self, client):
        resp = await client.post("/api/save-credentials", json={
            "client_id": "id",
            "client_secret": "",
        })
        assert resp.status == 400

    async def test_saves_region(self, client):
        import tesla_api
        resp = await client.post("/api/save-credentials", json={
            "client_id": "test-id",
            "client_secret": "test-secret",
            "region": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
        })
        assert resp.status == 200
        assert server.state["api_region"] == "https://fleet-api.prd.eu.vn.cloud.tesla.com"
        assert tesla_api.get_api_base() == "https://fleet-api.prd.eu.vn.cloud.tesla.com"


class TestOAuthUrl:
    async def test_requires_credentials(self, client):
        resp = await client.get("/api/oauth-url")
        data = await resp.json()
        assert "error" in data

    async def test_generates_url_with_state(self, client):
        server.state["client_id"] = "test-id"
        server.state["public_key_url"] = "https://example.com"
        resp = await client.get("/api/oauth-url")
        data = await resp.json()
        assert "url" in data
        assert "auth.tesla.com" in data["url"]
        assert server.state["oauth_state"] is not None


class TestOAuthCallback:
    async def test_rejects_missing_code(self, client):
        resp = await client.get("/oauth/callback")
        assert resp.status == 400

    async def test_rejects_invalid_state(self, client):
        server.state["oauth_state"] = "valid-state"
        resp = await client.get("/oauth/callback?code=test&state=wrong-state")
        assert resp.status == 400


class TestReset:
    async def test_resets_state(self, client, setup_complete_state):
        with patch.object(server.proxy_manager, "stop", new_callable=AsyncMock):
            resp = await client.post("/api/reset")
            assert resp.status == 200
            assert server.state["step"] == 1
            assert server.state["client_id"] is None
            assert server.state["tokens"] is None


class TestProxyEndpoints:
    async def test_proxy_status(self, client):
        resp = await client.get("/api/proxy/status")
        assert resp.status == 200
        data = await resp.json()
        assert "running" in data
        assert "available" in data

    async def test_proxy_start(self, client, mock_proxy_unavailable):
        with patch.object(server.proxy_manager, "start", new_callable=AsyncMock,
                          return_value={"success": False, "error": "binary not found"}):
            resp = await client.post("/api/proxy/start")
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is False

    async def test_proxy_stop(self, client):
        with patch.object(server.proxy_manager, "stop", new_callable=AsyncMock):
            resp = await client.post("/api/proxy/stop")
            assert resp.status == 200
            data = await resp.json()
            assert "running" in data


class TestWizardPage:
    async def test_serves_html(self, client):
        resp = await client.get("/")
        assert resp.status == 200
        assert "text/html" in resp.content_type

    async def test_injects_base_href(self, client):
        resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
        body = await resp.text()
        assert 'base href="/api/hassio_ingress/abc123/"' in body

    async def test_version_injected(self, client):
        resp = await client.get("/")
        body = await resp.text()
        # __VERSION__ should be replaced
        assert "__VERSION__" not in body


class TestRegionDetection:
    async def test_status_includes_region(self, client):
        server.state["api_region"] = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
        resp = await client.get("/api/status")
        data = await resp.json()
        assert data["api_region"] == "https://fleet-api.prd.eu.vn.cloud.tesla.com"

    async def test_status_region_null_by_default(self, client):
        resp = await client.get("/api/status")
        data = await resp.json()
        assert data["api_region"] is None

    async def test_region_restored_on_load(self, client):
        import tesla_api
        server.state["api_region"] = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
        server.save_state()
        server.load_state()
        # Simulate what on_startup does
        if server.state.get("api_region"):
            tesla_api.set_api_base(server.state["api_region"])
        assert tesla_api.get_api_base() == "https://fleet-api.prd.eu.vn.cloud.tesla.com"

    async def test_set_and_get_api_base(self):
        import tesla_api
        tesla_api.set_api_base("https://fleet-api.prd.eu.vn.cloud.tesla.com")
        assert tesla_api.get_api_base() == "https://fleet-api.prd.eu.vn.cloud.tesla.com"
        tesla_api.set_api_base(tesla_api.DEFAULT_API_BASE)
        assert tesla_api.get_api_base() == tesla_api.TESLA_API_BASE_NA


class TestStatePersistence:
    async def test_state_saved_to_disk(self, client, tmp_path):
        server.state["step"] = 3
        server.save_state()
        state_file = server.STATE_PATH
        assert state_file.exists()
        saved = json.loads(state_file.read_text())
        assert saved["step"] == 3

    async def test_state_file_permissions(self, client, tmp_path):
        import stat
        server.save_state()
        mode = server.STATE_PATH.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600
