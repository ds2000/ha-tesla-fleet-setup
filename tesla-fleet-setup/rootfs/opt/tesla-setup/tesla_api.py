"""Tesla Fleet API helpers for partner authentication and OAuth."""

import logging
import re
import ssl
import urllib.parse
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

TESLA_AUTH_BASE = "https://auth.tesla.com"
TESLA_API_BASE_NA = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TESLA_API_BASE_EU = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
DEFAULT_API_BASE = TESLA_API_BASE_NA

# Known valid Fleet API endpoints — used for region validation
VALID_FLEET_BASES = {TESLA_API_BASE_NA, TESLA_API_BASE_EU}

# Friendly names for display in the wizard
REGION_NAMES = {
    TESLA_API_BASE_NA: "North America, Asia-Pacific",
    TESLA_API_BASE_EU: "Europe, Middle East, Africa",
}
_TESLA_FLEET_RE = re.compile(r"^https://fleet-api\.prd\.[a-z]{2}\.vn\.cloud\.tesla\.com$")

PRIVATE_KEY_PATH = Path("/data/keys/private.pem")

# Module-level mutable region state — set after detection
_api_base: str = DEFAULT_API_BASE


def get_api_base() -> str:
    return _api_base


def set_api_base(url: str):
    global _api_base
    _api_base = url
    logger.info("Fleet API base set to %s", url)


def _validate_fleet_url(url: str) -> str | None:
    """Validate and normalize a Fleet API base URL. Returns URL or None if invalid."""
    if not url:
        return None
    if not url.startswith("https://"):
        url = f"https://{url}"
    if _TESLA_FLEET_RE.match(url):
        return url
    logger.warning("Rejected unexpected fleet URL: %s", url)
    return None


async def detect_region(access_token: str) -> str:
    """Auto-detect the user's Fleet API region. Returns the correct base URL."""
    for base in [TESLA_API_BASE_NA, TESLA_API_BASE_EU]:
        url = f"{base}/api/1/users/region"
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status in (200, 412):
                        data = await resp.json()
                        fleet_url = _validate_fleet_url(
                            data.get("response", {}).get("fleet_api_base_url", ""),
                        )
                        if fleet_url:
                            logger.info("Region detected: %s", fleet_url)
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


async def register_partner(client_id: str, client_secret: str, domain: str, api_base: str | None = None) -> dict:
    """
    Complete the partner authentication flow:
    1. Get a partner auth token from Tesla
    2. Register the partner account (triggers Tesla to verify .well-known/appkeys)

    Registers on the specified region endpoint only.
    """
    if api_base is None:
        api_base = _api_base

    result = await _register_partner_region(client_id, client_secret, domain, api_base)
    if result["success"]:
        logger.info("Partner registration successful on %s", api_base)
    return result


async def _register_partner_region(client_id: str, client_secret: str, domain: str, api_base: str) -> dict:
    """Register as a partner on a specific regional endpoint."""
    token_url = f"{TESLA_AUTH_BASE}/oauth2/v3/token"
    token_payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "openid user_data vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds",
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
            return {"success": True, "data": body, "region": api_base}


def get_oauth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Tesla OAuth authorization URL."""
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid offline_access user_data vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds energy_device_data energy_cmds",
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
            # Log response keys for debugging vehicle_data issues
            if "vehicle_data" in path:
                response = data.get("response", data)
                keys = [k for k in response if k not in ("id", "user_id", "vehicle_id", "vin", "color", "access_type", "granular_access")]
                logger.info("vehicle_data response keys: %s", keys)
            return {"success": True, "data": data}
        safe = _sanitize_error(str(data))
        logger.error("API request %s %s failed (HTTP %d): %s", method, path, resp.status, safe)
        return {"success": False, "status": resp.status, "error": safe}


async def list_vehicles(access_token: str) -> dict:
    """List all vehicles on the account."""
    return await _api_request(access_token, "GET", "/api/1/vehicles")


async def get_vehicle_data(access_token: str, vehicle_id: str) -> dict:
    """Get comprehensive vehicle data.

    Note: location_data is excluded because it requires vehicle_location scope
    which may not be granted. drive_state still includes GPS when available.
    """
    endpoints = "charge_state%3Bclimate_state%3Bdrive_state%3Bvehicle_state%3Bvehicle_config"
    return await _api_request(access_token, "GET",
                              f"/api/1/vehicles/{vehicle_id}/vehicle_data?endpoints={endpoints}")


async def wake_vehicle(access_token: str, vehicle_id: str) -> dict:
    """Wake up a vehicle."""
    return await _api_request(access_token, "POST", f"/api/1/vehicles/{vehicle_id}/wake_up", {})


async def send_command(access_token: str, vehicle_id: str, command: str, body: dict | None = None,
                       use_proxy: bool = False, vin: str | None = None) -> dict:
    """Send a command to a vehicle.

    If use_proxy=True, routes through the local tesla-http-proxy (port 4443)
    which signs commands using the Vehicle Command Protocol. The proxy
    requires a 17-character VIN, not the numeric Fleet API vehicle ID.
    """
    if use_proxy:
        if not vin:
            return {"success": False, "error": "VIN required for signed commands (proxy)"}
        return await _proxy_request(access_token, vin, command, body)
    return await _api_request(access_token, "POST",
                              f"/api/1/vehicles/{vehicle_id}/command/{command}", body or {})


async def _proxy_request(access_token: str, vin: str, command: str,
                          body: dict | None = None) -> dict:
    """Send a signed command through the local tesla-http-proxy.

    The proxy requires a 17-character VIN, not the numeric Fleet API vehicle ID.
    """
    url = f"https://localhost:4443/api/1/vehicles/{vin}/command/{command}"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        # Pin the self-signed TLS cert instead of disabling verification
        import proxy as _proxy_mod
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.load_verify_locations(str(_proxy_mod.TLS_CERT_PATH))
        ssl_ctx.check_hostname = False  # cert uses IP SAN, not hostname
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(url, headers=headers, json=body or {},
                                     timeout=aiohttp.ClientTimeout(total=15)) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = {"raw": await resp.text()}
                if resp.status == 200:
                    return {"success": True, "data": data}
                safe = _sanitize_error(str(data))
                logger.error("Proxy command %s failed (HTTP %d): %s", command, resp.status, safe)
                return {"success": False, "status": resp.status, "error": safe}
    except Exception as e:
        logger.error("Proxy command %s failed: %s", command, e)
        return {"success": False, "error": f"Proxy unavailable: {e}"}
