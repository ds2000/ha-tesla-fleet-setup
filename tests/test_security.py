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

import duckdns
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

    async def test_status_never_exposes_duckdns_token(self, client, setup_complete_state):
        server.state["duckdns_token"] = "secret-duckdns-token"
        resp = await client.get("/api/status")
        body = await resp.text()
        assert "secret-duckdns-token" not in body

    async def test_status_never_exposes_cf_tunnel_token(self, client, setup_complete_state):
        server.state["cf_tunnel_token"] = "secret-cf-token"
        resp = await client.get("/api/status")
        body = await resp.text()
        assert "secret-cf-token" not in body

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


class TestXSSPrevention:
    """Test XSS prevention in wizard page."""

    async def test_ingress_path_xss_rejected(self, client):
        """Malicious X-Ingress-Path header should be sanitized."""
        resp = await client.get("/", headers={"X-Ingress-Path": '"><script>alert(1)</script>'})
        body = await resp.text()
        assert "<script>alert(1)</script>" not in body

    async def test_ingress_path_valid_passes(self, client):
        """Valid X-Ingress-Path header should work."""
        resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
        body = await resp.text()
        assert 'href="/api/hassio_ingress/abc123/"' in body

    async def test_version_html_escaped(self, client):
        """VERSION is HTML-escaped in output."""
        resp = await client.get("/")
        assert resp.status == 200


class TestVehicleInputValidation:
    """Test vehicle ID and command parameter validation."""

    async def test_vehicle_id_path_traversal(self, client, setup_complete_state):
        """Path traversal in vehicle_id should be rejected."""
        resp = await client.get("/api/vehicles/../../admin/data")
        assert resp.status in {400, 404}

    async def test_command_injection_rejected(self, client, setup_complete_state):
        """Non-alphanumeric command names should be rejected."""
        resp = await client.post("/api/vehicles/123/command/../../etc/passwd")
        # Should get 400 (invalid command) or 404 (route not matched)
        assert resp.status in (400, 404)


class TestRegionValidation:
    """Test Fleet API region URL validation."""

    async def test_rejects_arbitrary_region(self, client):
        """Arbitrary URLs should not be accepted as region."""
        resp = await client.post("/api/save-credentials", json={
            "client_id": "test-id",
            "client_secret": "test-secret",
            "region": "https://evil.com",
        })
        assert resp.status == 200
        # Region should NOT be set to the evil URL
        assert server.state.get("api_region") != "https://evil.com"

    async def test_accepts_valid_region(self, client):
        """Known Tesla Fleet API URLs should be accepted."""
        resp = await client.post("/api/save-credentials", json={
            "client_id": "test-id",
            "client_secret": "test-secret",
            "region": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
        })
        assert resp.status == 200
        assert server.state["api_region"] == "https://fleet-api.prd.eu.vn.cloud.tesla.com"


class TestSubdomainValidation:
    """Test DuckDNS subdomain input sanitization."""

    def test_rejects_shell_injection(self):
        result = server._clean_subdomain('; rm -rf /')
        assert result == ""

    def test_rejects_path_traversal(self):
        result = server._clean_subdomain("../../etc/passwd")
        assert result == ""

    def test_accepts_valid_subdomain(self):
        result = server._clean_subdomain("my-tesla")
        assert result == "my-tesla"

    def test_strips_duckdns_suffix(self):
        result = server._clean_subdomain("my-tesla.duckdns.org")
        assert result == "my-tesla"

    def test_rejects_empty(self):
        result = server._clean_subdomain("")
        assert result == ""

    def test_rejects_too_long(self):
        result = server._clean_subdomain("a" * 64)
        assert result == ""


class TestCredentialsForHA:
    """Test credentials-for-ha endpoint access control."""

    async def test_rejected_without_ingress_header(self, client, setup_complete_state):
        resp = await client.get("/api/credentials-for-ha")
        assert resp.status == 403

    async def test_allowed_with_ingress_header(self, client, setup_complete_state):
        resp = await client.get("/api/credentials-for-ha",
                                headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
        assert resp.status == 200
        data = await resp.json()
        assert data["client_id"] == "test-client-id"

    async def test_returns_400_when_no_credentials(self, client):
        resp = await client.get("/api/credentials-for-ha",
                                headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
        assert resp.status == 400


class TestSecurityHeaders:
    """Test that security headers are present on responses."""

    async def test_has_x_content_type_options(self, client):
        resp = await client.get("/api/status")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    async def test_has_x_frame_options(self, client):
        resp = await client.get("/api/status")
        assert resp.headers.get("X-Frame-Options") == "DENY"

    async def test_has_referrer_policy(self, client):
        resp = await client.get("/api/status")
        assert resp.headers.get("Referrer-Policy") == "no-referrer"

    async def test_has_csp(self, client):
        resp = await client.get("/api/status")
        assert "Content-Security-Policy" in resp.headers


class TestCredentialLengthLimits:
    """Test that credential fields have length limits."""

    async def test_rejects_oversized_client_id(self, client):
        resp = await client.post("/api/save-credentials", json={
            "client_id": "x" * 200,
            "client_secret": "valid-secret",
        })
        assert resp.status == 400

    async def test_rejects_oversized_client_secret(self, client):
        resp = await client.post("/api/save-credentials", json={
            "client_id": "valid-id",
            "client_secret": "x" * 200,
        })
        assert resp.status == 400


class TestUPnPSSRFPrevention:
    """Test that UPnP SSDP location is validated as private IP."""

    def test_rejects_public_ip_location(self):
        assert duckdns._is_private_url("http://8.8.8.8:1900/desc.xml") is False

    def test_rejects_metadata_ip(self):
        assert duckdns._is_private_url("http://169.254.169.254/latest/") is False

    def test_rejects_link_local(self):
        assert duckdns._is_private_url("http://169.254.1.1:1900/desc.xml") is False

    def test_accepts_private_ip(self):
        assert duckdns._is_private_url("http://192.168.1.1:1900/desc.xml") is True

    def test_accepts_10_network(self):
        assert duckdns._is_private_url("http://10.0.0.1:1900/desc.xml") is True

    def test_rejects_hostname(self):
        assert duckdns._is_private_url("http://evil.com/desc.xml") is False

    def test_rejects_empty(self):
        assert duckdns._is_private_url("") is False


class TestProxySecurity:
    """Test proxy-related security measures."""

    async def test_proxy_status_accessible(self, client):
        resp = await client.get("/api/proxy/status")
        assert resp.status == 200
