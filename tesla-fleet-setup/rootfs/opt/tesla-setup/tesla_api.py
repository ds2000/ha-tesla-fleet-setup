"""Tesla Fleet API helpers for partner authentication and OAuth."""

import logging
import re
import urllib.parse
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

TESLA_AUTH_BASE = "https://auth.tesla.com"
TESLA_API_BASE_NA = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TESLA_API_BASE_EU = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
DEFAULT_API_BASE = TESLA_API_BASE_NA

PRIVATE_KEY_PATH = Path("/data/keys/private.pem")

# Module-level mutable region state — set after detection
_api_base: str = DEFAULT_API_BASE


def get_api_base() -> str:
    return _api_base


def set_api_base(url: str):
    global _api_base
    _api_base = url
    logger.info("Fleet API base set to %s", url)


async def detect_region(access_token: str) -> str:
    """Auto-detect the user's Fleet API region. Returns the correct base URL."""
    # Try NA first
    for base in [TESLA_API_BASE_NA, TESLA_API_BASE_EU]:
        url = f"{base}/api/1/users/region"
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        fleet_url = data.get("response", {}).get("fleet_api_base_url")
                        if fleet_url:
                            # Ensure https:// prefix
                            if not fleet_url.startswith("https://"):
                                fleet_url = f"https://{fleet_url}"
                            logger.info("Region detected: %s", fleet_url)
                            return fleet_url
                    elif resp.status == 412:
                        # Wrong region — the response tells us the correct one
                        data = await resp.json()
                        fleet_url = data.get("response", {}).get("fleet_api_base_url")
                        if fleet_url:
                            if not fleet_url.startswith("https://"):
                                fleet_url = f"https://{fleet_url}"
                            logger.info("Region redirect: %s", fleet_url)
                            return fleet_url
        except Exception as e:
            logger.debug("Region check against %s failed: %s", base, e)
            continue

    logger.warning("Could not detect region, defaulting to NA")
    return DEFAULT_API_BASE


def _sanitize_error(body: str) -> str:
    """Strip any credential-like values from error responses before logging or returning."""
    # Never log raw bodies — they may echo back client_secret, tokens, or codes
    if len(body) > 300:
        body = body[:300] + "...(truncated)"
    # Redact anything that looks like a token or secret
    body = re.sub(r'"(access_token|refresh_token|client_secret|code)"\s*:\s*"[^"]*"',
                  r'"\1":"[REDACTED]"', body)
    return body


async def register_partner(client_id: str, client_secret: str, domain: str) -> dict:
    """
    Complete the partner authentication flow:
    1. Get a partner auth token from Tesla
    2. Register the partner account (triggers Tesla to verify .well-known/appkeys)

    Registers against both NA and EU endpoints to cover all regions.
    """
    results = []
    for api_base in [TESLA_API_BASE_NA, TESLA_API_BASE_EU]:
        result = await _register_partner_region(client_id, client_secret, domain, api_base)
        if result["success"]:
            results.append(result)
        else:
            logger.info("Partner registration on %s: %s", api_base, result.get("error", "failed"))

    if results:
        logger.info("Partner registration successful on %d region(s)", len(results))
        return results[0]

    # Both failed — return a helpful error
    return {"success": False, "step": "register", "error": "Registration failed on all regions. Check your credentials and that the public key URL is accessible."}


async def _register_partner_region(client_id: str, client_secret: str, domain: str, api_base: str) -> dict:
    """Register as a partner on a specific regional endpoint."""
    token_url = f"{TESLA_AUTH_BASE}/oauth2/v3/token"
    token_payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "openid vehicle_device_data vehicle_cmds vehicle_charging_cmds",
        "audience": api_base,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, json=token_payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                safe = _sanitize_error(body)
                logger.error("Token request failed (HTTP %d) for %s", resp.status, api_base)
                return {"success": False, "step": "token", "status": resp.status, "error": safe}
            token_data = await resp.json()

        access_token = token_data["access_token"]

        register_url = f"{api_base}/api/1/partner_accounts"
        register_payload = {"domain": domain}
        headers = {"Authorization": f"Bearer {access_token}"}

        async with session.post(register_url, json=register_payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text() if resp.content_type != "application/json" else str(await resp.json())
                safe = _sanitize_error(body)
                logger.error("Partner registration failed (HTTP %d) on %s: %s", resp.status, api_base, safe)
                return {"success": False, "step": "register", "status": resp.status, "error": safe}

            body = await resp.json()
            logger.info("Partner registration successful on %s", api_base)
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

    async with aiohttp.ClientSession() as session, session.post(token_url, json=payload) as resp:
        if resp.status != 200:
            body = await resp.text()
            safe = _sanitize_error(body)
            logger.error("Token exchange failed (HTTP %d)", resp.status)
            return {"success": False, "error": safe}
        data = await resp.json()
        logger.info("Token exchange successful")
        return {"success": True, "data": data}


async def refresh_tokens(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Refresh an expired access token."""
    token_url = f"{TESLA_AUTH_BASE}/oauth2/v3/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }

    async with aiohttp.ClientSession() as session, session.post(token_url, json=payload) as resp:
        if resp.status != 200:
            body = await resp.text()
            safe = _sanitize_error(body)
            logger.error("Token refresh failed (HTTP %d)", resp.status)
            return {"success": False, "error": safe}
        data = await resp.json()
        logger.info("Token refresh successful")
        return {"success": True, "data": data}


async def _api_request(access_token: str, method: str, path: str, json_body: dict | None = None) -> dict:
    """Make an authenticated request to the Tesla Fleet API.

    Handles 412 (wrong region) by auto-detecting and retrying once.
    """
    url = f"{_api_base}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with aiohttp.ClientSession() as session, session.request(method, url, headers=headers, json=json_body,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
        # Handle region mismatch — 412 means wrong regional endpoint
        if resp.status == 412:
            logger.warning("Got 412 (wrong region) — auto-detecting correct region")
            new_base = await detect_region(access_token)
            if new_base != _api_base:
                set_api_base(new_base)
                # Retry with correct region
                retry_url = f"{_api_base}{path}"
                async with aiohttp.ClientSession() as s2, s2.request(method, retry_url, headers=headers,
                                   json=json_body, timeout=aiohttp.ClientTimeout(total=15)) as resp2:
                    try:
                        data = await resp2.json()
                    except Exception:
                        data = {"raw": _sanitize_error(await resp2.text())}
                    if resp2.status == 200:
                        return {"success": True, "data": data}
                    safe = _sanitize_error(str(data))
                    logger.error("API request %s %s failed after region switch (HTTP %d)", method, path, resp2.status)
                    return {"success": False, "status": resp2.status, "error": safe}

        try:
            data = await resp.json()
        except Exception:
            data = {"raw": _sanitize_error(await resp.text())}
        if resp.status == 200:
            return {"success": True, "data": data}
        safe = _sanitize_error(str(data))
        logger.error("API request %s %s failed (HTTP %d)", method, path, resp.status)
        return {"success": False, "status": resp.status, "error": safe}


async def list_vehicles(access_token: str) -> dict:
    """List all vehicles on the account."""
    return await _api_request(access_token, "GET", "/api/1/vehicles")


async def get_vehicle_data(access_token: str, vehicle_id: str) -> dict:
    """Get comprehensive vehicle data."""
    endpoints = "charge_state,climate_state,drive_state,location_data,vehicle_state,vehicle_config"
    return await _api_request(access_token, "GET",
                              f"/api/1/vehicles/{vehicle_id}/vehicle_data?endpoints={endpoints}")


async def wake_vehicle(access_token: str, vehicle_id: str) -> dict:
    """Wake up a vehicle."""
    return await _api_request(access_token, "POST", f"/api/1/vehicles/{vehicle_id}/wake_up", {})


async def send_command(access_token: str, vehicle_id: str, command: str, body: dict | None = None) -> dict:
    """Send a command to a vehicle."""
    return await _api_request(access_token, "POST",
                              f"/api/1/vehicles/{vehicle_id}/command/{command}", body or {})
