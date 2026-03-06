#!/usr/bin/env python3
"""Run the Tesla Fleet Setup wizard locally for development/testing.

Usage:
  python3 run_local.py          # Normal mode (real API calls)
  python3 run_local.py --demo   # Demo mode (all external calls mocked)

Then open: http://localhost:8099/
"""

import os
import sys
import shutil
from pathlib import Path

DEMO_MODE = "--demo" in sys.argv

REPO_DIR = Path(__file__).parent
SRC_DIR = REPO_DIR / "tesla-fleet-setup" / "rootfs" / "opt" / "tesla-setup"

# Local data directory (instead of /data inside Docker)
LOCAL_DATA = REPO_DIR / ".local_data"
LOCAL_DATA.mkdir(exist_ok=True)
(LOCAL_DATA / "keys").mkdir(exist_ok=True)

# Copy screenshots into static dir (mimic what Dockerfile COPY does)
STATIC_SS = SRC_DIR / "static" / "screenshots"
REPO_SS = REPO_DIR / "screenshots"
if REPO_SS.exists():
    if STATIC_SS.is_symlink():
        STATIC_SS.unlink()
    STATIC_SS.mkdir(exist_ok=True)
    for f in REPO_SS.glob("*.png"):
        shutil.copy2(f, STATIC_SS / f.name)

# Add the source directory to sys.path
sys.path.insert(0, str(SRC_DIR))

# Patch keygen paths
import keygen
keygen.KEYS_DIR = LOCAL_DATA / "keys"
keygen.PRIVATE_KEY_PATH = keygen.KEYS_DIR / "private.pem"
keygen.PUBLIC_KEY_PATH = keygen.KEYS_DIR / "public.pem"

# Patch server state path
import server
server.STATE_PATH = LOCAL_DATA / "state.json"

PORT = int(os.environ.get("PORT", "8099"))

# --- Demo mode: mock all external calls ---
if DEMO_MODE:
    import asyncio
    import ha_discovery
    import tesla_api

    # 1. Mock HA discovery — report external URL as localhost
    #    (so the verify self-test can actually reach our own .well-known endpoint)
    async def _mock_ha_info():
        return {
            "external_url": f"http://localhost:{PORT}",
            "has_nabu_casa": True,
            "has_external_url": True,
        }
    ha_discovery.get_ha_info = _mock_ha_info

    # 2. Mock tunnel — return localhost URL
    class MockTunnel:
        url = None
        running = False
        async def start(self, port):
            await asyncio.sleep(2)
            self.url = f"http://localhost:{port}"
            self.running = True
            return self.url
        async def stop(self):
            self.running = False
            self.url = None
    server.tunnel_manager = MockTunnel()

    # 3. Mock partner registration — always succeed after delay
    async def _mock_register(client_id, client_secret, domain):
        await asyncio.sleep(1.5)
        return {"success": True, "data": {"account_id": "demo-partner-123"}}
    tesla_api.register_partner = _mock_register

    # 4. Mock token exchange — always succeed
    async def _mock_exchange(client_id, client_secret, code, redirect_uri):
        return {
            "success": True,
            "data": {
                "access_token": "demo-access-token-xxxxx",
                "refresh_token": "demo-refresh-token-xxxxx",
                "token_type": "bearer",
                "expires_in": 28800,
            },
        }
    tesla_api.exchange_code = _mock_exchange

    # 5. Mock OAuth URL — go straight to our own callback with a fake code
    def _mock_get_oauth_url(client_id, redirect_uri, state):
        return f"http://localhost:{PORT}/oauth/callback?code=demo-auth-code&state={state}"
    tesla_api.get_oauth_url = _mock_get_oauth_url


if __name__ == "__main__":
    from aiohttp import web

    mode = "DEMO MODE" if DEMO_MODE else "normal"
    print(f"\n  Tesla Fleet Setup Wizard ({mode})")
    print(f"  http://localhost:{PORT}/")
    print(f"  Data: {LOCAL_DATA}/")
    if DEMO_MODE:
        print(f"  All external calls mocked — click through the full flow")
    print()

    # Clear state for a fresh demo run
    if DEMO_MODE:
        state_file = LOCAL_DATA / "state.json"
        if state_file.exists():
            state_file.unlink()

    app = server.create_app()
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
