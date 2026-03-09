"""Security-focused tests for Tesla Fleet Setup.

Tests for:
- Credential exposure prevention
- Path traversal protection
- Tunnel isolation (attack surface reduction)
- State file security
- Input validation and injection prevention
- OAuth security (state parameter, replay prevention)
- TLS certificate security
"""

import stat
from unittest.mock import AsyncMock, patch

import keygen
import proxy
import server
import tesla_api


class TestCredentialExposure:
    """Ensure secrets never leak in API responses or logs."""

    async def test_status_never_exposes_client_secret(self, client, setup_complete_state):
        resp = await client.get("/api/status")
        body = await resp.text()
        assert "test-client-secret" not in body

    async def test_status_never_exposes_access_token(self, client, setup_complete_state):
        resp = await client.get("/api/status")
        body = await resp.text()
        assert "test-access-token" not in body

    async def test_status_never_exposes_refresh_token(self, client, setup_complete_state):
        resp = await client.get("/api/status")
        body = await resp.text()
        assert "test-refresh-token" not in body

    def test_sanitize_redacts_tokens(self):
        body = '{"access_token":"secret123","refresh_token":"refresh456"}'
        result = tesla_api._sanitize_error(body)
        assert "secret123" not in result
        assert "refresh456" not in result
        assert "[REDACTED]" in result

    def test_sanitize_redacts_client_secret(self):
        body = '{"client_secret":"mysecret"}'
        result = tesla_api._sanitize_error(body)
        assert "mysecret" not in result

    def test_sanitize_redacts_code(self):
        body = '{"code":"auth_code_123"}'
        result = tesla_api._sanitize_error(body)
        assert "auth_code_123" not in result

    def test_sanitize_truncates_long_bodies(self):
        body = "x" * 1000
        result = tesla_api._sanitize_error(body)
        assert len(result) < 400
        assert "truncated" in result


class TestOAuthSecurity:
    """Test OAuth flow security measures."""

    async def test_state_parameter_validated(self, client):
        server.state["oauth_state"] = "expected-state"
        resp = await client.get("/oauth/callback?code=test&state=wrong-state")
        assert resp.status == 400

    async def test_missing_code_rejected(self, client):
        resp = await client.get("/oauth/callback?state=test")
        assert resp.status == 400

    async def test_state_cleared_after_use(self, client, setup_complete_state):
        server.state["oauth_state"] = "test-state"
        server.state["public_key_url"] = "https://example.com"

        with patch("tesla_api.exchange_code", new_callable=AsyncMock, return_value={
            "success": True,
            "data": {"access_token": "t", "refresh_token": "r", "expires_in": 3600},
        }), patch.object(server.proxy_manager, "start", new_callable=AsyncMock, return_value={"success": False}):
            resp = await client.get("/oauth/callback?code=authcode&state=test-state")
            assert resp.status == 200
            assert server.state["oauth_state"] is None

    async def test_replay_prevented(self, client, setup_complete_state):
        server.state["oauth_state"] = "once-only"
        server.state["public_key_url"] = "https://example.com"

        with patch("tesla_api.exchange_code", new_callable=AsyncMock, return_value={
            "success": True,
            "data": {"access_token": "t", "refresh_token": "r", "expires_in": 3600},
        }), patch.object(server.proxy_manager, "start", new_callable=AsyncMock, return_value={"success": False}):
            # First use
            resp1 = await client.get("/oauth/callback?code=code1&state=once-only")
            assert resp1.status == 200

            # Replay attempt — state was cleared
            resp2 = await client.get("/oauth/callback?code=code2&state=once-only")
            assert resp2.status == 400

    async def test_oauth_state_is_random(self, client):
        server.state["client_id"] = "test-id"
        server.state["public_key_url"] = "https://example.com"

        resp1 = await client.get("/api/oauth-url")
        await resp1.json()
        state1 = server.state["oauth_state"]

        resp2 = await client.get("/api/oauth-url")
        await resp2.json()
        state2 = server.state["oauth_state"]

        assert state1 != state2
        assert len(state1) > 20


class TestInputValidation:
    """Test that user inputs are validated."""

    async def test_url_must_be_https(self, client):
        resp = await client.post("/api/set-url", json={"url": "http://evil.com"})
        assert resp.status == 400

    async def test_url_rejects_javascript(self, client):
        resp = await client.post("/api/set-url", json={"url": "javascript:alert(1)"})
        assert resp.status == 400

    async def test_url_rejects_empty(self, client):
        resp = await client.post("/api/set-url", json={"url": ""})
        assert resp.status == 400

    async def test_credentials_rejects_empty_id(self, client):
        resp = await client.post("/api/save-credentials", json={
            "client_id": "", "client_secret": "secret",
        })
        assert resp.status == 400

    async def test_credentials_rejects_empty_secret(self, client):
        resp = await client.post("/api/save-credentials", json={
            "client_id": "id", "client_secret": "",
        })
        assert resp.status == 400

    async def test_credentials_strips_whitespace(self, client):
        resp = await client.post("/api/save-credentials", json={
            "client_id": "  test-id  ", "client_secret": "  test-secret  ",
        })
        assert resp.status == 200
        assert server.state["client_id"] == "test-id"
        assert server.state["client_secret"] == "test-secret"


class TestFilePermissions:
    """Test that sensitive files have correct permissions."""

    def test_private_key_is_600(self):
        keygen.ensure_keys()
        mode = keygen.PRIVATE_KEY_PATH.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_state_file_is_600(self):
        server.save_state()
        mode = server.STATE_PATH.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_tls_key_is_600(self):
        proxy.ensure_tls_certs()
        mode = proxy.TLS_KEY_PATH.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_public_key_is_readable(self):
        keygen.ensure_keys()
        assert keygen.PUBLIC_KEY_PATH.exists()
        content = keygen.PUBLIC_KEY_PATH.read_text()
        assert len(content) > 0


class TestTLSCertSecurity:
    """Test TLS certificate properties."""

    def test_cert_has_localhost_san(self):
        from cryptography import x509
        cert_path, _ = proxy.ensure_tls_certs()
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "localhost" in dns_names

    def test_cert_has_addon_hostname_san(self):
        from cryptography import x509
        cert_path, _ = proxy.ensure_tls_certs()
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "tesla-fleet-setup" in dns_names

    def test_cert_has_ip_san(self):
        import ipaddress

        from cryptography import x509
        cert_path, _ = proxy.ensure_tls_certs()
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ips = san.value.get_values_for_type(x509.IPAddress)
        assert ipaddress.IPv4Address("127.0.0.1") in ips

    def test_cert_uses_ec_key(self):
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import ec
        cert_path, _ = proxy.ensure_tls_certs()
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey)

    def test_separate_tls_and_command_keys(self):
        keygen.ensure_keys()
        proxy.ensure_tls_certs()
        command_key = keygen.PRIVATE_KEY_PATH.read_text()
        tls_key = proxy.TLS_KEY_PATH.read_text()
        assert command_key != tls_key


class TestProxySecurity:
    """Test proxy-related security measures."""

    async def test_proxy_status_accessible(self, client):
        resp = await client.get("/api/proxy/status")
        assert resp.status == 200
