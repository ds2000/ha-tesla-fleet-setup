"""Main aiohttp server: wizard UI, .well-known endpoint, and API routes."""

import asyncio
import html as html_mod
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import cf_api
import duckdns
import ha_credentials
import ha_discovery
import keygen
import proxy
import tesla_api
import tunnel
from aiohttp import web

# --- Logging setup: suppress credential leaks ---

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("tesla-setup")

# Suppress aiohttp access logs (they would log OAuth callback URLs with auth codes)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

PORT = int(os.environ.get("INGRESS_PORT", "8099"))
VERSION = os.environ.get("BUILD_VERSION", "dev")
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
STATE_PATH = Path("/data/state.json")

HA_CONFIG_KEY = Path("/config/tesla_fleet.key")


def install_key_to_ha():
    """Copy private key to /config/tesla_fleet.key for the HA Tesla Fleet integration."""
    src = keygen.PRIVATE_KEY_PATH
    if not src.exists():
        logger.warning("Private key not found at %s — cannot install to HA config", src)
        return False
    try:
        shutil.copy2(src, HA_CONFIG_KEY)
        os.chmod(HA_CONFIG_KEY, 0o600)
        logger.info("Private key installed to %s", HA_CONFIG_KEY)
        return True
    except Exception as e:
        logger.error("Failed to install key to HA config: %s", e)
        return False


proxy_manager = proxy.ProxyManager()

# In-memory state (persisted to /data/state.json)
state = {
    "step": 1,
    "public_key_url": None,
    "url_method": None,  # "duckdns", "manual"
    "client_id": None,
    "client_secret": None,
    "partner_registered": False,
    "oauth_state": None,
    "tokens": None,
    "api_region": None,  # Detected Fleet API base URL
    "duckdns_subdomain": None,
    "duckdns_token": None,
}


def load_state():
    global state
    if STATE_PATH.exists():
        try:
            saved = json.loads(STATE_PATH.read_text())
            state.update(saved)
        except Exception:
            logger.warning("Failed to load state from %s — starting fresh", STATE_PATH)


def save_state():
    """Write state to disk with restricted permissions (contains secrets)."""
    fd = tempfile.NamedTemporaryFile(
        mode="w", dir=STATE_PATH.parent, suffix=".tmp", delete=False,
    )
    try:
        fd.write(json.dumps(state, indent=2))
        fd.close()
        os.chmod(fd.name, 0o600)
        os.replace(fd.name, STATE_PATH)
    except BaseException:
        fd.close()
        os.unlink(fd.name)
        raise


# Lock to prevent concurrent token refreshes
_token_lock = asyncio.Lock()


# --- .well-known endpoint ---

async def well_known_appkeys(request):
    """Serve the public key at Tesla's expected .well-known path."""
    public_pem = keygen.get_public_key()
    return web.Response(text=public_pem, content_type="text/plain")


# --- API routes ---

async def api_status(request):
    """Return current wizard state (never exposes secrets)."""
    return web.json_response({
        "step": state["step"],
        "public_key_url": state["public_key_url"],
        "url_method": state["url_method"],
        "partner_registered": state["partner_registered"],
        "has_credentials": state["client_id"] is not None,
        "has_tokens": state["tokens"] is not None,
        "ha_credentials_injected": state.get("ha_credentials_injected", False),
        "api_region": state.get("api_region"),
        "proxy": proxy_manager.get_status(),
        "duckdns_configured": state.get("duckdns_subdomain") is not None,
        "duckdns_subdomain": state.get("duckdns_subdomain"),
        "duckdns_https_running": duckdns.https_running(),
        "duckdns_has_cert": duckdns.has_cert(),
        "cf_tunnel_domain": state.get("cf_tunnel_domain"),
        "cf_tunnel_running": tunnel.is_running(),
    })


async def api_generate_keys(request):
    """Generate keys and detect external URL."""
    _private_pem, public_pem = keygen.ensure_keys()
    ha_info = await ha_discovery.get_ha_info()

    result = {"public_key": public_pem, "ha_info": ha_info}

    state["step"] = max(state["step"], 2)
    save_state()

    result["url_method"] = state["url_method"]
    result["public_key_url"] = state["public_key_url"]
    return web.json_response(result)


async def api_set_url(request):
    """Manually set the public key URL."""
    data = await request.json()
    url = data.get("url", "").strip().rstrip("/")
    if not url or not url.startswith("https://"):
        return web.json_response({"error": "URL must start with https://"}, status=400)
    state["public_key_url"] = url
    state["url_method"] = "manual"
    save_state()
    logger.info("Public key URL set manually: %s", url)
    return web.json_response({"url": url})


async def api_verify_url(request):
    """Self-test: verify .well-known/appkeys is reachable from the internet.

    Always tests the actual public URL — not localhost. This catches
    double NAT, ISP port blocking, and DNS propagation issues.
    """
    url = state.get("public_key_url")
    if not url:
        return web.json_response({"error": "No public URL configured"}, status=400)

    test_url = f"{url}/.well-known/appspecific/com.tesla.3p.public-key.pem"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(test_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    body = await resp.text()
                    if "BEGIN PUBLIC KEY" in body:
                        return web.json_response({"verified": True, "url": test_url})
                return web.json_response({"verified": False, "status": resp.status})
    except Exception as e:
        logger.warning("URL verification failed: %s", e)
        return web.json_response({"verified": False, "error": "Connection failed — check DNS and network"})


async def api_save_credentials(request):
    """Save Tesla app client_id, client_secret, and region."""
    data = await request.json()
    client_id = data.get("client_id", "").strip()
    client_secret = data.get("client_secret", "").strip()
    region = data.get("region", "").strip()

    if not client_id or not client_secret:
        return web.json_response({"error": "Both client_id and client_secret are required"}, status=400)

    state["client_id"] = client_id
    state["client_secret"] = client_secret
    state["step"] = max(state["step"], 4)

    # Set Fleet API region if provided — must be a known Tesla endpoint
    if region and region in tesla_api.VALID_FLEET_BASES:
        state["api_region"] = region
        tesla_api.set_api_base(region)
        logger.info("Fleet API region set to %s", region)

    save_state()
    logger.info("Credentials saved (client_id: %s...)", client_id[:8])
    return web.json_response({"success": True})


async def api_register_partner(request):
    """Register as a Tesla partner (triggers .well-known verification)."""
    if not state["client_id"] or not state["client_secret"]:
        return web.json_response({"error": "Credentials not set"}, status=400)
    if not state["public_key_url"]:
        return web.json_response({"error": "Public key URL not set"}, status=400)

    domain = urlparse(state["public_key_url"]).hostname

    api_base = state.get("api_region")
    result = await tesla_api.register_partner(state["client_id"], state["client_secret"], domain, api_base)

    if result["success"]:
        state["partner_registered"] = True
        state["step"] = max(state["step"], 5)
        save_state()
        # Include friendly region name in response
        region_url = result.get("region", api_base or tesla_api.get_api_base())
        result["region_name"] = tesla_api.REGION_NAMES.get(region_url, region_url)

    return web.json_response(result)


async def api_get_oauth_url(request):
    """Generate the Tesla OAuth authorization URL."""
    if not state["client_id"]:
        return web.json_response({"error": "Credentials not set"}, status=400)

    oauth_state = secrets.token_urlsafe(32)
    state["oauth_state"] = oauth_state
    save_state()

    redirect_uri = f"{state['public_key_url']}/oauth/callback"
    url = tesla_api.get_oauth_url(state["client_id"], redirect_uri, oauth_state)
    return web.json_response({"url": url, "redirect_uri": redirect_uri})


async def oauth_callback(request):
    """Handle OAuth callback from Tesla."""
    code = request.query.get("code")
    returned_state = request.query.get("state")

    if not code:
        return web.Response(text="Missing authorization code", status=400)
    if returned_state != state.get("oauth_state"):
        return web.Response(text="Invalid state parameter", status=400)

    redirect_uri = f"{state['public_key_url']}/oauth/callback"
    result = await tesla_api.exchange_code(state["client_id"], state["client_secret"], code, redirect_uri)

    if result["success"]:
        token_data = result["data"]
        token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)
        state["tokens"] = token_data
        state["step"] = 6
        # Clear oauth_state so it can't be replayed
        state["oauth_state"] = None
        save_state()
        logger.info("OAuth complete — tokens saved")

        # Auto-detect Fleet API region (NA vs EU)
        try:
            region_base = await tesla_api.detect_region(token_data["access_token"])
            tesla_api.set_api_base(region_base)
            state["api_region"] = region_base
            save_state()
        except Exception as e:
            logger.warning("Region detection failed: %s", e)

        # Inject credentials into HA Application Credentials store
        cred_result = await ha_credentials.inject_credentials(
            state["client_id"], state["client_secret"]
        )
        if cred_result["success"]:
            state["ha_credentials_injected"] = True
            save_state()
            if cred_result.get("already_exists"):
                logger.info("Application Credentials already present in HA")
            else:
                logger.info("Application Credentials injected into HA")
        else:
            logger.warning("Could not inject credentials: %s", cred_result.get("error"))

        # Install private key to /config/tesla_fleet.key for HA integration
        install_key_to_ha()

        # Auto-start the signing proxy now that setup is complete
        if proxy.proxy_available():
            proxy_result = await proxy_manager.start()
            if proxy_result["success"]:
                logger.info("Signing proxy started after OAuth completion")
            else:
                logger.warning("Proxy start failed: %s", proxy_result.get("error"))
        # Return success page directly — don't redirect, because the
        # tunnel guard would block /?setup=complete
        return web.Response(
            text=(
                "<!DOCTYPE html><html><head>"
                "<meta charset='UTF-8'>"
                "<style>body{background:#0d0d0d;color:#e0e0e0;font-family:system-ui;"
                "display:flex;align-items:center;justify-content:center;height:100vh;"
                "text-align:center;margin:0}"
                ".ok{background:#2ecc71;width:72px;height:72px;border-radius:50%;"
                "display:flex;align-items:center;justify-content:center;font-size:2rem;"
                "margin:0 auto 20px}</style></head><body><div>"
                "<div class='ok'>&#10003;</div>"
                "<h2>Tesla Connected!</h2>"
                "<p style='color:#888;margin-top:12px'>You can close this tab and return "
                "to the Tesla Fleet Setup add-on in Home Assistant.</p>"
                "</div></body></html>"
            ),
            content_type="text/html",
        )
    # Never expose raw error detail to browser
    logger.error("Token exchange failed")
    return web.Response(
        text="Token exchange failed. Check the add-on logs for details.",
        status=500,
    )


async def _get_access_token():
    """Get a valid access token, refreshing if needed. Uses lock to prevent concurrent refreshes."""
    async with _token_lock:
        tokens = state.get("tokens")
        if not tokens:
            return None, "No tokens available. Complete setup first."

        # Check if token is expired (with 60s buffer)
        expires_at = tokens.get("expires_at", 0)
        if not expires_at and tokens.get("expires_in"):
            expires_at = 0

        if expires_at and time.time() < expires_at - 60:
            return tokens["access_token"], None

        # Token expired or unknown expiry — try refresh
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            return None, "No refresh token. Please reconnect via OAuth."

        result = await tesla_api.refresh_tokens(
            state["client_id"], state["client_secret"], refresh_token
        )
        if result["success"]:
            new_tokens = result["data"]
            new_tokens["expires_at"] = time.time() + new_tokens.get("expires_in", 3600)
            state["tokens"] = new_tokens
            save_state()
            return new_tokens["access_token"], None
        return None, f"Token refresh failed: {result.get('error', 'unknown')}"


async def api_vehicles(request):
    """List vehicles on the account."""
    token, err = await _get_access_token()
    if err:
        return web.json_response({"success": False, "error": err}, status=401)
    result = await tesla_api.list_vehicles(token)
    return web.json_response(result)


_VEHICLE_ID_RE = re.compile(r"^\d+$")
_COMMAND_RE = re.compile(r"^[a-z0-9_]+$")


def _validate_vehicle_id(vid: str) -> str | None:
    """Return error message if vehicle_id is invalid, else None."""
    if not _VEHICLE_ID_RE.match(vid):
        return "Invalid vehicle ID"
    return None


# Cache vehicle ID → VIN mapping (populated from list_vehicles)
_vin_cache: dict[str, str] = {}


async def _resolve_vin(access_token: str, vehicle_id: str) -> str | None:
    """Resolve a numeric vehicle ID to a 17-character VIN.

    The signing proxy requires VIN, not the Fleet API numeric ID.
    """
    if vehicle_id in _vin_cache:
        return _vin_cache[vehicle_id]
    result = await tesla_api.list_vehicles(access_token)
    if result.get("success"):
        for v in result["data"].get("response", []):
            vid = str(v.get("id", ""))
            vin = v.get("vin", "")
            if vid and vin:
                _vin_cache[vid] = vin
    return _vin_cache.get(vehicle_id)


async def api_vehicle_data(request):
    """Get full vehicle data."""
    token, err = await _get_access_token()
    if err:
        return web.json_response({"success": False, "error": err}, status=401)
    vid = request.match_info["vehicle_id"]
    if verr := _validate_vehicle_id(vid):
        return web.json_response({"success": False, "error": verr}, status=400)
    result = await tesla_api.get_vehicle_data(token, vid)
    return web.json_response(result)


async def api_wake(request):
    """Wake up a vehicle."""
    token, err = await _get_access_token()
    if err:
        return web.json_response({"success": False, "error": err}, status=401)
    vid = request.match_info["vehicle_id"]
    if verr := _validate_vehicle_id(vid):
        return web.json_response({"success": False, "error": verr}, status=400)
    result = await tesla_api.wake_vehicle(token, vid)
    return web.json_response(result)


async def api_command(request):
    """Send a command to a vehicle, routing through signing proxy when available."""
    token, err = await _get_access_token()
    if err:
        return web.json_response({"success": False, "error": err}, status=401)
    vid = request.match_info["vehicle_id"]
    cmd = request.match_info["command"]
    if verr := _validate_vehicle_id(vid):
        return web.json_response({"success": False, "error": verr}, status=400)
    if not _COMMAND_RE.match(cmd):
        return web.json_response({"success": False, "error": "Invalid command"}, status=400)
    try:
        body = await request.json()
    except Exception:
        body = None
    # Route through signing proxy if it's running (required for Vehicle Command Protocol)
    use_proxy = proxy_manager.get_status().get("running", False)
    vin = None
    if use_proxy:
        vin = await _resolve_vin(token, vid)
    result = await tesla_api.send_command(token, vid, cmd, body, use_proxy=use_proxy, vin=vin)
    return web.json_response(result)


async def api_proxy_status(request):
    """Return proxy status."""
    return web.json_response(proxy_manager.get_status())


async def api_proxy_start(request):
    """Start the tesla-http-proxy."""
    result = await proxy_manager.start()
    return web.json_response(result)


async def api_proxy_stop(request):
    """Stop the tesla-http-proxy."""
    await proxy_manager.stop()
    return web.json_response({"success": True, **proxy_manager.get_status()})


async def api_inject_credentials(request):
    """Manually inject credentials into HA Application Credentials."""
    if not state["client_id"] or not state["client_secret"]:
        return web.json_response({"success": False, "error": "No credentials saved"}, status=400)
    result = await ha_credentials.inject_credentials(state["client_id"], state["client_secret"])
    if result["success"]:
        state["ha_credentials_injected"] = True
        save_state()
    return web.json_response(result)


async def api_credentials_for_ha(request):
    """Return client_id and client_secret for manual HA credential entry.

    Only served via HA ingress (same-machine access). Used as fallback
    when automatic injection fails.
    """
    if not state["client_id"] or not state["client_secret"]:
        return web.json_response({"error": "No credentials saved"}, status=400)
    return web.json_response({
        "client_id": state["client_id"],
        "client_secret": state["client_secret"],
    })


_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _clean_subdomain(raw: str) -> str:
    """Strip .duckdns.org suffix, validate as safe DNS subdomain label."""
    s = raw.strip().lower()
    for suffix in [".duckdns.org", ".duckdns"]:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = s.strip(".")
    if not s or not _SUBDOMAIN_RE.match(s) or len(s) > 63:
        return ""
    return s


async def api_duckdns_verify(request):
    """Verify DuckDNS credentials work."""
    data = await request.json()
    subdomain = _clean_subdomain(data.get("subdomain", ""))
    token = data.get("token", "").strip()
    if not subdomain or not token:
        return web.json_response({"success": False, "error": "Subdomain and token required"}, status=400)
    result = await duckdns.verify_token(subdomain, token)
    return web.json_response(result)


async def api_duckdns_upnp(request):
    """Try UPnP port forwarding for port 443."""
    result = await duckdns.open_port_upnp()
    return web.json_response(result)


async def api_duckdns_port_test(request):
    """Diagnostic: open external port 8443 → internal 443 via UPnP to test ISP blocking."""
    result = await duckdns.open_port_upnp(port=443, ext_port=8443)
    return web.json_response(result)


async def api_duckdns_ip(request):
    """Update DuckDNS IP address."""
    data = await request.json()
    subdomain = _clean_subdomain(data.get("subdomain", ""))
    token = data.get("token", "").strip()
    if not subdomain or not token:
        return web.json_response({"success": False, "error": "Subdomain and token required"}, status=400)
    ip_ok = await duckdns.update_ip(subdomain, token)
    if not ip_ok:
        return web.json_response({"success": False, "error": "DuckDNS IP update failed — check token"}, status=400)
    return web.json_response({"success": True})


async def api_duckdns_cert(request):
    """Obtain Let's Encrypt certificate via DNS-01."""
    data = await request.json()
    subdomain = _clean_subdomain(data.get("subdomain", ""))
    token = data.get("token", "").strip()
    if not subdomain or not token:
        return web.json_response({"success": False, "error": "Subdomain and token required"}, status=400)
    result = await duckdns.obtain_cert(subdomain, token)
    return web.json_response(result, status=200 if result["success"] else 500)


async def api_duckdns_start(request):
    """Start HTTPS server + background services and save state."""
    data = await request.json()
    subdomain = _clean_subdomain(data.get("subdomain", ""))
    token = data.get("token", "").strip()
    if not subdomain or not token:
        return web.json_response({"success": False, "error": "Subdomain and token required"}, status=400)

    https_ok = await duckdns.start_https_server()
    if not https_ok:
        return web.json_response({"success": False, "error": "Failed to start HTTPS server"}, status=500)
    duckdns.start_updater(subdomain, token)
    duckdns.start_renewal_checker(subdomain, token)

    domain = f"{subdomain}.duckdns.org"
    public_url = f"https://{domain}"
    state["duckdns_subdomain"] = subdomain
    state["duckdns_token"] = token
    state["public_key_url"] = public_url
    state["url_method"] = "duckdns"
    save_state()

    logger.info("DuckDNS configured: %s (HTTPS serving .well-known)", domain)
    return web.json_response({"success": True, "url": public_url, "domain": domain})


async def api_cloudflare_start(request):
    """Start a Cloudflare named tunnel."""
    data = await request.json()
    token = data.get("token", "").strip()
    domain = data.get("domain", "").strip().lower()
    if not token or not domain:
        return web.json_response({"success": False, "error": "Token and domain are required"}, status=400)

    result = await tunnel.start(token, domain)
    if result["success"]:
        public_url = f"https://{domain}"
        state["public_key_url"] = public_url
        state["url_method"] = "cloudflare"
        state["cf_tunnel_token"] = token
        state["cf_tunnel_domain"] = domain
        state["step"] = max(state.get("step", 1), 2)
        save_state()
    return web.json_response(result)


async def api_cloudflare_zones(request):
    """Fetch Cloudflare zones (domains) for the given API token."""
    data = await request.json()
    api_token = data.get("api_token", "").strip()
    if not api_token:
        return web.json_response({"success": False, "error": "API token required"}, status=400)

    cf = cf_api.CloudflareAPI(api_token)
    verify = await cf.verify_token()
    if not verify["success"]:
        return web.json_response({"success": False, "error": verify["error"]}, status=401)

    zones = await cf.get_zones()
    if not zones:
        return web.json_response({"success": False, "error": "No domains found on this Cloudflare account"})
    return web.json_response({"success": True, "zones": zones})


async def api_cloudflare_auto_setup(request):
    """Step-by-step Cloudflare tunnel setup. Returns completed_steps list for progress UI."""
    data = await request.json()
    api_token = data.get("api_token", "").strip()
    zone_id = data.get("zone_id", "").strip()
    zone_name = data.get("zone_name", "").strip()
    subdomain = data.get("subdomain", "tesla").strip().lower()
    account_id = data.get("account_id", "").strip() or None

    if not api_token or not zone_id or not zone_name:
        return web.json_response({"success": False, "error": "Missing required fields"}, status=400)

    cf = cf_api.CloudflareAPI(api_token)
    completed = []

    # Step 1: Verify token
    verify = await cf.verify_token()
    if not verify["success"]:
        return web.json_response({"success": False, "error": verify["error"],
                                   "step": "verify", "completed_steps": completed})
    completed.append("verify")

    # Step 2: Create tunnel
    if not account_id:
        account_id = await cf.get_account_id()
    if not account_id:
        zones = await cf.get_zones()
        for z in zones:
            if z.get("account_id"):
                account_id = z["account_id"]
                break
    if not account_id:
        return web.json_response({"success": False, "error": "No Cloudflare account found",
                                   "step": "tunnel", "completed_steps": completed})

    tunnel_result = await cf.create_or_reuse_tunnel(account_id, "ha-tesla-fleet")
    if "error" in tunnel_result:
        return web.json_response({"success": False, "error": tunnel_result["error"],
                                   "step": "tunnel", "completed_steps": completed})
    tunnel_id = tunnel_result["id"]
    tunnel_token = tunnel_result["token"]
    completed.append("tunnel")

    hostname = f"{subdomain}.{zone_name}" if subdomain else zone_name

    # Step 3: Configure tunnel ingress
    config = await cf.configure_tunnel(account_id, tunnel_id, hostname)
    if not config["success"]:
        return web.json_response({"success": False, "error": config["error"],
                                   "step": "config", "completed_steps": completed})
    completed.append("config")

    # Step 4: Create DNS record
    dns = await cf.create_dns_record(zone_id, subdomain, tunnel_id, zone_name)
    if not dns["success"]:
        return web.json_response({"success": False, "error": dns["error"],
                                   "step": "dns", "completed_steps": completed})
    completed.append("dns")

    # Step 5: Start cloudflared
    start_result = await tunnel.start(tunnel_token, hostname)
    if not start_result["success"]:
        return web.json_response({
            "success": False,
            "error": start_result.get("error", "cloudflared failed to start"),
            "step": "start", "completed_steps": completed,
        })
    completed.append("start")

    # Save state
    public_url = f"https://{hostname}"
    state["public_key_url"] = public_url
    state["url_method"] = "cloudflare"
    state["cf_tunnel_token"] = tunnel_token
    state["cf_tunnel_domain"] = hostname
    state["step"] = max(state.get("step", 1), 2)
    save_state()

    return web.json_response({"success": True, "domain": hostname, "completed_steps": completed})


async def api_cloudflare_stop(request):
    """Stop the Cloudflare tunnel."""
    await tunnel.stop()
    return web.json_response({"success": True})


async def api_cloudflare_status(request):
    """Get Cloudflare tunnel status."""
    return web.json_response(tunnel.status())


async def api_reset(request):
    """Reset wizard state (start over)."""
    global state
    state = {
        "step": 1,
        "public_key_url": None,
        "url_method": None,
        "client_id": None,
        "client_secret": None,
        "partner_registered": False,
        "oauth_state": None,
        "tokens": None,
        "duckdns_subdomain": None,
        "duckdns_token": None,
    }
    save_state()
    await proxy_manager.stop()
    await duckdns.stop_all()
    await tunnel.stop()
    logger.info("Wizard state reset")
    return web.json_response({"success": True})


_INGRESS_PATH_RE = re.compile(r"^/[a-zA-Z0-9/_-]*$")


async def wizard_page(request):
    """Serve the wizard HTML.

    Injects a <base href> using the X-Ingress-Path header so that
    relative URLs (api/status, static/...) resolve through the HA
    ingress proxy instead of hitting HA Core directly.
    """
    page = (TEMPLATES_DIR / "wizard.html").read_text()
    ingress_path = request.headers.get("X-Ingress-Path", "")
    # Validate ingress path to prevent XSS injection
    if ingress_path and not _INGRESS_PATH_RE.match(ingress_path):
        ingress_path = ""
    base_url = f"{ingress_path}/" if ingress_path else "/"
    page = page.replace("<head>", f'<head>\n  <base href="{html_mod.escape(base_url)}">', 1)
    page = page.replace("__VERSION__", html_mod.escape(VERSION))
    return web.Response(text=page, content_type="text/html")


async def on_startup(app):
    """Generate keys on startup and auto-start proxy if setup is complete."""
    load_state()
    keygen.ensure_keys()
    logger.info("Keys ready. Wizard available on port %d", PORT)

    # Restore detected Fleet API region
    if state.get("api_region"):
        tesla_api.set_api_base(state["api_region"])
        logger.info("Restored Fleet API region: %s", state["api_region"])

    # Install key to HA config if setup was previously completed
    if state.get("tokens") and not HA_CONFIG_KEY.exists():
        install_key_to_ha()

    # Auto-start DuckDNS if configured (stable domain for Tesla key verification)
    if state.get("duckdns_subdomain") and state.get("duckdns_token"):
        subdomain = state["duckdns_subdomain"]
        token = state["duckdns_token"]
        try:
            await duckdns.start_all(subdomain, token)
            logger.info("DuckDNS active: %s.duckdns.org (HTTPS + IP updater)", subdomain)
        except Exception as e:
            logger.warning("DuckDNS startup failed: %s", e)

    # Auto-start Cloudflare tunnel if configured
    if state.get("cf_tunnel_token") and state.get("cf_tunnel_domain"):
        cf_token = state["cf_tunnel_token"]
        cf_domain = state["cf_tunnel_domain"]
        result = await tunnel.start(cf_token, cf_domain)
        if result["success"]:
            logger.info("Cloudflare tunnel active: %s", cf_domain)
        else:
            logger.warning("Cloudflare tunnel startup failed: %s", result.get("error"))

    # Auto-start proxy if setup was previously completed
    if state.get("tokens") and proxy.proxy_available():
        logger.info("Setup previously completed — starting signing proxy")
        result = await proxy_manager.start()
        if result["success"]:
            logger.info("Signing proxy running on port %d", proxy.PROXY_PORT)
        else:
            logger.warning("Proxy auto-start failed: %s", result.get("error"))


async def on_shutdown(app):
    """Clean up proxy and DuckDNS on shutdown."""
    await proxy_manager.stop()
    await duckdns.stop_all()
    await tunnel.stop()


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Tesla fetches: /.well-known/appspecific/com.tesla.3p.public-key.pem
    app.router.add_get("/.well-known/appspecific/com.tesla.3p.public-key.pem", well_known_appkeys)

    # OAuth callback (also at root for redirect URI)
    app.router.add_get("/oauth/callback", oauth_callback)

    # All routes at root — HA ingress proxy strips the ingress prefix
    app.router.add_get("/", wizard_page)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/generate-keys", api_generate_keys)
    app.router.add_post("/api/set-url", api_set_url)
    app.router.add_post("/api/verify-url", api_verify_url)
    app.router.add_post("/api/save-credentials", api_save_credentials)
    app.router.add_post("/api/register-partner", api_register_partner)
    app.router.add_get("/api/oauth-url", api_get_oauth_url)
    app.router.add_post("/api/reset", api_reset)

    # HA integration
    app.router.add_post("/api/inject-credentials", api_inject_credentials)
    app.router.add_get("/api/credentials-for-ha", api_credentials_for_ha)

    # DuckDNS
    app.router.add_post("/api/duckdns/verify", api_duckdns_verify)
    app.router.add_post("/api/duckdns/upnp", api_duckdns_upnp)
    app.router.add_post("/api/duckdns/port-test", api_duckdns_port_test)
    app.router.add_post("/api/duckdns/ip", api_duckdns_ip)
    app.router.add_post("/api/duckdns/cert", api_duckdns_cert)
    app.router.add_post("/api/duckdns/start", api_duckdns_start)

    # Cloudflare tunnel
    app.router.add_post("/api/cloudflare/zones", api_cloudflare_zones)
    app.router.add_post("/api/cloudflare/auto-setup", api_cloudflare_auto_setup)
    app.router.add_post("/api/cloudflare/start", api_cloudflare_start)
    app.router.add_post("/api/cloudflare/stop", api_cloudflare_stop)
    app.router.add_get("/api/cloudflare/status", api_cloudflare_status)

    # Proxy management
    app.router.add_get("/api/proxy/status", api_proxy_status)
    app.router.add_post("/api/proxy/start", api_proxy_start)
    app.router.add_post("/api/proxy/stop", api_proxy_stop)

    # API testing endpoints
    app.router.add_get("/api/vehicles", api_vehicles)
    app.router.add_get("/api/vehicles/{vehicle_id}/data", api_vehicle_data)
    app.router.add_post("/api/vehicles/{vehicle_id}/wake", api_wake)
    app.router.add_post("/api/vehicles/{vehicle_id}/command/{command}", api_command)

    # Static files
    app.router.add_static("/static", STATIC_DIR)

    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
