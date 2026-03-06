"""Main aiohttp server: wizard UI, .well-known endpoint, and API routes."""

import json
import logging
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

import keygen
import tunnel
import tesla_api
import ha_discovery

# --- Logging setup: suppress credential leaks ---

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("tesla-setup")

# Suppress aiohttp access logs (they would log OAuth callback URLs with auth codes)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

INGRESS_PATH = os.environ.get("INGRESS_PATH", "")
PORT = int(os.environ.get("INGRESS_PORT", "8099"))
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
STATE_PATH = Path("/data/state.json")

tunnel_manager = tunnel.TunnelManager()

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
    """Serve the public key at /.well-known/appkeys."""
    public_pem = keygen.get_public_key()
    return web.Response(text=public_pem, content_type="application/x-pem-file")


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
    })


async def api_generate_keys(request):
    """Generate keys and detect external URL."""
    _private_pem, public_pem = keygen.ensure_keys()
    ha_info = await ha_discovery.get_ha_info()

    result = {"public_key": public_pem, "ha_info": ha_info}

    # Auto-select URL method
    if ha_info["has_nabu_casa"]:
        state["public_key_url"] = ha_info["external_url"]
        state["url_method"] = "nabu_casa"
    elif ha_info["has_external_url"]:
        state["public_key_url"] = ha_info["external_url"]
        state["url_method"] = "external_url"

    state["step"] = max(state["step"], 2)
    save_state()

    result["url_method"] = state["url_method"]
    result["public_key_url"] = state["public_key_url"]
    return web.json_response(result)


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
    """Self-test: fetch our own .well-known/appkeys via the public URL."""
    url = state.get("public_key_url")
    if not url:
        return web.json_response({"error": "No public URL configured"}, status=400)

    test_url = f"{url}/.well-known/appkeys"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(test_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    body = await resp.text()
                    if "BEGIN PUBLIC KEY" in body:
                        return web.json_response({"verified": True, "url": test_url})
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
        state["tokens"] = result["data"]
        state["step"] = 6
        # Clear oauth_state so it can't be replayed
        state["oauth_state"] = None
        save_state()
        logger.info("OAuth complete — tokens saved")
        raise web.HTTPFound(f"{INGRESS_PATH}/?setup=complete")
    else:
        # Never expose raw error detail to browser
        logger.error("Token exchange failed")
        return web.Response(
            text="Token exchange failed. Check the add-on logs for details.",
            status=500,
        )


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
    logger.info("Wizard state reset")
    return web.json_response({"success": True})


async def wizard_page(request):
    """Serve the wizard HTML."""
    html = (TEMPLATES_DIR / "wizard.html").read_text()
    html = html.replace("__INGRESS_PATH__", INGRESS_PATH)
    return web.Response(text=html, content_type="text/html")


async def on_startup(app):
    """Generate keys on startup."""
    load_state()
    keygen.ensure_keys()
    logger.info("Keys ready. Wizard available on port %d", PORT)


async def on_shutdown(app):
    """Clean up tunnel on shutdown."""
    await tunnel_manager.stop()


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # .well-known must be at the root (for tunnel/direct access)
    app.router.add_get("/.well-known/appkeys", well_known_appkeys)

    # OAuth callback (also at root for redirect URI)
    app.router.add_get("/oauth/callback", oauth_callback)

    # Ingress-aware routes (HA proxies through INGRESS_PATH)
    prefix = INGRESS_PATH if INGRESS_PATH else ""
    app.router.add_get(f"{prefix}/", wizard_page)
    app.router.add_get(f"{prefix}/api/status", api_status)
    app.router.add_post(f"{prefix}/api/generate-keys", api_generate_keys)
    app.router.add_post(f"{prefix}/api/start-tunnel", api_start_tunnel)
    app.router.add_post(f"{prefix}/api/verify-url", api_verify_url)
    app.router.add_post(f"{prefix}/api/save-credentials", api_save_credentials)
    app.router.add_post(f"{prefix}/api/register-partner", api_register_partner)
    app.router.add_get(f"{prefix}/api/oauth-url", api_get_oauth_url)
    app.router.add_post(f"{prefix}/api/reset", api_reset)

    # Static files
    app.router.add_static(f"{prefix}/static", STATIC_DIR)

    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
