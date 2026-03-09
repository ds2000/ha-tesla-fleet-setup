"""Main aiohttp server: wizard UI, .well-known endpoint, and API routes."""

import json
import logging
import os
import secrets
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
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


tunnel_manager = tunnel.TunnelManager()
proxy_manager = proxy.ProxyManager()

# In-memory state (persisted to /data/state.json)
state = {
    "step": 1,
    "public_key_url": None,
    "url_method": None,  # "nabu_casa", "external_url", "tunnel"
    "client_id": None,
    "client_secret": None,
    "partner_registered": False,
    "oauth_state": None,
    "tokens": None,
    "api_region": None,  # Detected Fleet API base URL
}


def load_state():
    global state
    if STATE_PATH.exists():
        try:
            saved = json.loads(STATE_PATH.read_text())
            state.update(saved)
        except Exception:
            pass


def save_state():
    """Write state to disk with restricted permissions (contains secrets)."""
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)
    os.chmod(STATE_PATH, 0o600)


# --- .well-known endpoint (served on all paths for tunnel) ---

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
    })


async def api_generate_keys(request):
    """Generate keys and detect external URL."""
    _private_pem, public_pem = keygen.ensure_keys()
    ha_info = await ha_discovery.get_ha_info()

    result = {"public_key": public_pem, "ha_info": ha_info}

    # Always use tunnel — Nabu Casa and external URLs point to HA Core,
    # which doesn't serve /.well-known/appspecific/com.tesla.3p.public-key.pem. Only our add-on does,
    # so we need a direct tunnel to port 8099.
    # The tunnel is temporary and only needed during setup.

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


async def api_start_tunnel(request):
    """Start a Cloudflare quick tunnel."""
    try:
        url = await tunnel_manager.start(PORT)
        state["public_key_url"] = url
        state["url_method"] = "tunnel"
        save_state()
        return web.json_response({"url": url})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_verify_url(request):
    """Self-test: verify .well-known/appkeys is being served.

    For tunnel URLs, test localhost directly (container can't resolve its
    own tunnel hostname). For external URLs, test via the public URL.
    """
    url = state.get("public_key_url")
    if not url:
        return web.json_response({"error": "No public URL configured"}, status=400)

    # For tunnel URLs, test localhost — the tunnel proxies to us, but we
    # can't resolve the trycloudflare.com hostname from inside the container.
    if ".trycloudflare.com" in url:
        test_url = f"http://localhost:{PORT}/.well-known/appspecific/com.tesla.3p.public-key.pem"
    else:
        test_url = f"{url}/.well-known/appspecific/com.tesla.3p.public-key.pem"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(test_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    body = await resp.text()
                    if "BEGIN PUBLIC KEY" in body:
                        well_known = f"{url}/.well-known/appspecific/com.tesla.3p.public-key.pem"
                        return web.json_response({"verified": True, "url": well_known})
                return web.json_response({"verified": False, "status": resp.status})
    except Exception as e:
        return web.json_response({"verified": False, "error": str(e)})


async def api_save_credentials(request):
    """Save Tesla app client_id and client_secret."""
    data = await request.json()
    client_id = data.get("client_id", "").strip()
    client_secret = data.get("client_secret", "").strip()

    if not client_id or not client_secret:
        return web.json_response({"error": "Both client_id and client_secret are required"}, status=400)

    state["client_id"] = client_id
    state["client_secret"] = client_secret
    state["step"] = max(state["step"], 4)
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

    result = await tesla_api.register_partner(state["client_id"], state["client_secret"], domain)

    if result["success"]:
        state["partner_registered"] = True
        state["step"] = max(state["step"], 5)
        save_state()

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
    """Get a valid access token, refreshing if needed."""
    tokens = state.get("tokens")
    if not tokens:
        return None, "No tokens available. Complete setup first."

    # Check if token is expired (with 60s buffer)
    expires_at = tokens.get("expires_at", 0)
    if not expires_at and tokens.get("expires_in"):
        # First time: estimate from creation time (not persisted, so refresh to be safe)
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


async def api_vehicle_data(request):
    """Get full vehicle data."""
    token, err = await _get_access_token()
    if err:
        return web.json_response({"success": False, "error": err}, status=401)
    vid = request.match_info["vehicle_id"]
    result = await tesla_api.get_vehicle_data(token, vid)
    return web.json_response(result)


async def api_wake(request):
    """Wake up a vehicle."""
    token, err = await _get_access_token()
    if err:
        return web.json_response({"success": False, "error": err}, status=401)
    vid = request.match_info["vehicle_id"]
    result = await tesla_api.wake_vehicle(token, vid)
    return web.json_response(result)


async def api_command(request):
    """Send a command to a vehicle (unsigned, security level 7)."""
    token, err = await _get_access_token()
    if err:
        return web.json_response({"success": False, "error": err}, status=401)
    vid = request.match_info["vehicle_id"]
    cmd = request.match_info["command"]
    try:
        body = await request.json()
    except Exception:
        body = None
    result = await tesla_api.send_command(token, vid, cmd, body)
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
    }
    save_state()
    await tunnel_manager.stop()
    await proxy_manager.stop()
    logger.info("Wizard state reset")
    return web.json_response({"success": True})


async def wizard_page(request):
    """Serve the wizard HTML.

    Injects a <base href> using the X-Ingress-Path header so that
    relative URLs (api/status, static/...) resolve through the HA
    ingress proxy instead of hitting HA Core directly.
    """
    html = (TEMPLATES_DIR / "wizard.html").read_text()
    ingress_path = request.headers.get("X-Ingress-Path", "")
    base_url = f"{ingress_path}/" if ingress_path else "/"
    html = html.replace("<head>", f'<head>\n  <base href="{base_url}">', 1)
    html = html.replace("__VERSION__", VERSION)
    return web.Response(text=html, content_type="text/html")


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

    # Auto-restart tunnel if setup used a tunnel (HA integration needs it for domain verification)
    if state.get("tokens") and state.get("url_method") == "tunnel":
        try:
            url = await tunnel_manager.start(PORT)
            old_url = state.get("public_key_url")
            if url != old_url:
                logger.warning(
                    "Tunnel restarted with NEW URL: %s (was %s). "
                    "You may need to re-register with Tesla if the domain changed.",
                    url, old_url,
                )
                state["public_key_url"] = url
                save_state()
            else:
                logger.info("Tunnel restarted: %s", url)
        except Exception as e:
            logger.warning("Failed to restart tunnel: %s", e)

    # Auto-start proxy if setup was previously completed
    if state.get("tokens") and proxy.proxy_available():
        logger.info("Setup previously completed — starting signing proxy")
        result = await proxy_manager.start()
        if result["success"]:
            logger.info("Signing proxy running on port %d", proxy.PROXY_PORT)
        else:
            logger.warning("Proxy auto-start failed: %s", result.get("error"))


async def on_shutdown(app):
    """Clean up tunnel and proxy on shutdown."""
    await tunnel_manager.stop()
    await proxy_manager.stop()


TUNNEL_ALLOWED_PATHS = {
    "/.well-known/appspecific/com.tesla.3p.public-key.pem",
    "/oauth/callback",
}


@web.middleware
async def tunnel_guard(request, handler):
    """Block non-allowed paths when accessed through the Cloudflare tunnel.

    Only /.well-known/appspecific/com.tesla.3p.public-key.pem and /oauth/callback are exposed to the internet.
    All other paths (wizard UI, API endpoints) return 404 when accessed via tunnel.
    """
    host = request.headers.get("Host", "")
    if ".trycloudflare.com" in host and request.path not in TUNNEL_ALLOWED_PATHS:
        raise web.HTTPNotFound()
    return await handler(request)


def create_app() -> web.Application:
    app = web.Application(middlewares=[tunnel_guard])
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # .well-known must be at the root (for tunnel/direct access)
    # Tesla fetches: /.well-known/appspecific/com.tesla.3p.public-key.pem
    app.router.add_get("/.well-known/appspecific/com.tesla.3p.public-key.pem", well_known_appkeys)

    # OAuth callback (also at root for redirect URI)
    app.router.add_get("/oauth/callback", oauth_callback)

    # All routes at root — HA ingress proxy strips the ingress prefix
    app.router.add_get("/", wizard_page)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/generate-keys", api_generate_keys)
    app.router.add_post("/api/set-url", api_set_url)
    app.router.add_post("/api/start-tunnel", api_start_tunnel)
    app.router.add_post("/api/verify-url", api_verify_url)
    app.router.add_post("/api/save-credentials", api_save_credentials)
    app.router.add_post("/api/register-partner", api_register_partner)
    app.router.add_get("/api/oauth-url", api_get_oauth_url)
    app.router.add_post("/api/reset", api_reset)

    # HA integration
    app.router.add_post("/api/inject-credentials", api_inject_credentials)

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
