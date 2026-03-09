"""Shared fixtures for Tesla Fleet Setup tests."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Add source directory to path
SRC_DIR = Path(__file__).parent.parent / "tesla-fleet-setup" / "rootfs" / "opt" / "tesla-setup"
sys.path.insert(0, str(SRC_DIR))


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Isolate all state/key paths to tmp_path so tests don't touch real data."""
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    state_file = tmp_path / "state.json"

    import keygen
    monkeypatch.setattr(keygen, "KEYS_DIR", keys_dir)
    monkeypatch.setattr(keygen, "PRIVATE_KEY_PATH", keys_dir / "private.pem")
    monkeypatch.setattr(keygen, "PUBLIC_KEY_PATH", keys_dir / "public.pem")

    import proxy
    monkeypatch.setattr(proxy, "TLS_DIR", tls_dir)
    monkeypatch.setattr(proxy, "TLS_CERT_PATH", tls_dir / "cert.pem")
    monkeypatch.setattr(proxy, "TLS_KEY_PATH", tls_dir / "key.pem")
    monkeypatch.setattr(proxy, "COMMAND_KEY_PATH", keys_dir / "private.pem")

    import server
    monkeypatch.setattr(server, "STATE_PATH", state_file)

    # Reset server state
    server.state = {
        "step": 1,
        "public_key_url": None,
        "url_method": None,
        "client_id": None,
        "client_secret": None,
        "partner_registered": False,
        "oauth_state": None,
        "tokens": None,
        "api_region": None,
    }

    # Reset tesla_api region to default
    import tesla_api
    tesla_api.set_api_base(tesla_api.DEFAULT_API_BASE)


@pytest.fixture
def mock_proxy_available(monkeypatch):
    """Mock proxy as available."""
    import proxy
    monkeypatch.setattr(proxy, "proxy_available", lambda: True)


@pytest.fixture
def mock_proxy_unavailable(monkeypatch):
    """Mock proxy as unavailable."""
    import proxy
    monkeypatch.setattr(proxy, "proxy_available", lambda: False)


@pytest.fixture
async def app():
    """Create the aiohttp application for testing."""
    import server
    # Patch on_startup to skip auto-start
    with patch.object(server.proxy_manager, "start", new_callable=AsyncMock):
        application = server.create_app()
        yield application


@pytest.fixture
async def client(aiohttp_client, app):
    """Create a test client."""
    return await aiohttp_client(app)


@pytest.fixture
def setup_complete_state(tmp_path):
    """Set up state as if wizard completed."""
    import server
    server.state.update({
        "step": 6,
        "public_key_url": "https://test.example.com",
        "url_method": "duckdns",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "partner_registered": True,
        "tokens": {
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "expires_in": 3600,
            "expires_at": 9999999999,
        },
    })
