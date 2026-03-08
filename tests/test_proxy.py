"""Tests for tesla-http-proxy management."""

import os
import stat
from unittest.mock import patch

import proxy
import pytest


class TestProxyAvailability:
    def test_unavailable_when_no_binary(self, monkeypatch):
        monkeypatch.setattr(proxy, "PROXY_BINARY", "/nonexistent/path")
        with patch("shutil.which", return_value=None):
            assert proxy.proxy_available() is False

    def test_available_when_binary_exists(self, tmp_path, monkeypatch):
        binary = tmp_path / "tesla-http-proxy"
        binary.write_text("#!/bin/sh\n")
        monkeypatch.setattr(proxy, "PROXY_BINARY", str(binary))
        assert proxy.proxy_available() is True


class TestTLSCerts:
    def test_generates_tls_certs(self):
        cert_path, key_path = proxy.ensure_tls_certs()
        assert os.path.exists(cert_path)
        assert os.path.exists(key_path)

    def test_tls_key_permissions(self):
        _, key_path = proxy.ensure_tls_certs()
        mode = os.stat(key_path).st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_tls_cert_is_valid_pem(self):
        cert_path, _ = proxy.ensure_tls_certs()
        content = open(cert_path).read()
        assert "BEGIN CERTIFICATE" in content

    def test_tls_key_is_valid_pem(self):
        _, key_path = proxy.ensure_tls_certs()
        content = open(key_path).read()
        assert "BEGIN PRIVATE KEY" in content

    def test_idempotent(self):
        cert1, key1 = proxy.ensure_tls_certs()
        cert2, key2 = proxy.ensure_tls_certs()
        assert cert1 == cert2
        assert key1 == key2
        # Content should be the same
        assert open(cert1).read() == open(cert2).read()

    def test_cert_has_san(self):
        from cryptography import x509
        cert_path, _ = proxy.ensure_tls_certs()
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "localhost" in dns_names
        assert "tesla-fleet-setup" in dns_names


class TestProxyManager:
    def test_initial_state(self):
        mgr = proxy.ProxyManager()
        assert mgr.running is False
        status = mgr.get_status()
        assert status["running"] is False
        assert status["url"] is None

    @pytest.mark.asyncio
    async def test_start_without_binary(self, mock_proxy_unavailable):
        mgr = proxy.ProxyManager()
        result = await mgr.start()
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_start_without_keys(self, mock_proxy_available, monkeypatch):
        monkeypatch.setattr(proxy, "COMMAND_KEY_PATH", proxy.TLS_DIR / "nonexistent.pem")
        mgr = proxy.ProxyManager()
        result = await mgr.start()
        assert result["success"] is False
        assert "key not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self):
        mgr = proxy.ProxyManager()
        await mgr.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_get_status_fields(self, mock_proxy_unavailable):
        mgr = proxy.ProxyManager()
        status = mgr.get_status()
        assert "running" in status
        assert "available" in status
        assert "port" in status
        assert "url" in status
        assert status["port"] == 4443
