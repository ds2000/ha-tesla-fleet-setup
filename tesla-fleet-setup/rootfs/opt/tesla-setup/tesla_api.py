"""Tesla Fleet API helpers for partner authentication and OAuth."""

import logging
import urllib.parse
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

TESLA_AUTH_BASE = "https://auth.tesla.com"
TESLA_API_BASE = "https://fleet-api.prd.na.vn.cloud.tesla.com"

PRIVATE_KEY_PATH = Path("/data/keys/private.pem")


def _sanitize_error(body: str) -> str:
    """Strip any credential-like values from error responses before logging or returning."""
    # Never log raw bodies — they may echo back client_secret, tokens, or codes
    if len(body) > 300:
        body = body[:300] + "...(truncated)"
    # Redact anything that looks like a token or secret
    import re
    body = re.sub(r'"(access_token|refresh_token|client_secret|code)"\s*:\s*"[^"]*"',
                  r'"\1":"[REDACTED]"', body)
    return body


async def register_partner(client_id: str, client_secret: str, domain: str) -> dict:
    """
    Complete the partner authentication flow:
    1. Get a partner auth token from Tesla
    2. Register the partner account (triggers Tesla to verify .well-known/appkeys)
    """
    # Step 1: Get partner authentication token
    token_url = f"{TESLA_AUTH_BASE}/oauth2/v3/token"
    token_payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "openid vehicle_device_data vehicle_cmds vehicle_charging_cmds",
        "audience": TESLA_API_BASE,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, json=token_payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                safe = _sanitize_error(body)
                logger.error("Token request failed (HTTP %d)", resp.status)
                return {"success": False, "step": "token", "status": resp.status, "error": safe}
            token_data = await resp.json()

        access_token = token_data["access_token"]

        # Step 2: Register partner account
        register_url = f"{TESLA_API_BASE}/api/1/partner_accounts"
        register_payload = {"domain": domain}
        headers = {"Authorization": f"Bearer {access_token}"}

        async with session.post(register_url, json=register_payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text() if resp.content_type != "application/json" else str(await resp.json())
                safe = _sanitize_error(body)
                logger.error("Partner registration failed (HTTP %d)", resp.status)
                return {"success": False, "step": "register", "status": resp.status, "error": safe}

            body = await resp.json()
            logger.info("Partner registration successful")
            return {"success": True, "data": body}


def get_oauth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Tesla OAuth authorization URL."""
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid offline_access vehicle_device_data vehicle_cmds vehicle_charging_cmds energy_device_data energy_cmds",
        "state": state,
    })
    return f"{TESLA_AUTH_BASE}/oauth2/v3/authorize?{params}"


async def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Exchange an OAuth authorization code for tokens."""
    token_url = f"{TESLA_AUTH_BASE}/oauth2/v3/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                safe = _sanitize_error(body)
                logger.error("Token exchange failed (HTTP %d)", resp.status)
                return {"success": False, "error": safe}
            data = await resp.json()
            logger.info("Token exchange successful")
            return {"success": True, "data": data}
