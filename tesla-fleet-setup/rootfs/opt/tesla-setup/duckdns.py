"""DuckDNS integration — stable public hostname for Tesla Fleet API key verification.

Manages:
  - DuckDNS IP updates (periodic, every 5 min)
  - Let's Encrypt certificate via DNS-01 challenge (certbot + DuckDNS API)
  - HTTPS server on port 443 serving .well-known/appkeys

This replaces Cloudflare quick tunnels which are ephemeral — Tesla periodically
verifies the .well-known endpoint and revokes the key if it's unreachable.
"""

import asyncio
import logging
import os
import shutil
import ssl
from pathlib import Path

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

DUCKDNS_UPDATE_URL = "https://www.duckdns.org/update"

# Certificate paths
CERT_DIR = Path("/data/letsencrypt")
CERT_PATH = CERT_DIR / "fullchain.pem"
KEY_PATH = CERT_DIR / "privkey.pem"

# Key paths
PUBLIC_KEY_PATH = Path("/data/keys/public.pem")

HTTPS_PORT = 443

_updater_task = None
_renewal_task = None
_https_runner = None


# ── DuckDNS IP update ───────────────────────────────────────────────────────

async def update_ip(subdomain: str, token: str) -> bool:
    """Update DuckDNS to point to the current public IP."""
    url = f"{DUCKDNS_UPDATE_URL}?domains={subdomain}&token={token}&verbose=true"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                ok = text.strip().startswith("OK")
                if ok:
                    # Extract IP from verbose response (OK\n1.2.3.4\nUPDATED)
                    lines = text.strip().split("\n")
                    ip = lines[1] if len(lines) > 1 else "unknown"
                    logger.info("DuckDNS updated: %s.duckdns.org -> %s", subdomain, ip)
                else:
                    logger.error("DuckDNS update failed: %s", text.strip())
                return ok
    except Exception as e:
        logger.error("DuckDNS update error: %s", e)
        return False


async def verify_token(subdomain: str, token: str) -> dict:
    """Verify DuckDNS credentials work. Returns {success, ip?, error?}."""
    url = f"{DUCKDNS_UPDATE_URL}?domains={subdomain}&token={token}&verbose=true"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
                if text.strip().startswith("OK"):
                    lines = text.strip().split("\n")
                    ip = lines[1] if len(lines) > 1 else None
                    return {"success": True, "ip": ip}
                return {"success": False, "error": "Invalid token or subdomain"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _updater_loop(subdomain: str, token: str):
    """Background loop updating DuckDNS IP every 5 minutes."""
    while True:
        await update_ip(subdomain, token)
        await asyncio.sleep(300)


def start_updater(subdomain: str, token: str):
    """Start the periodic IP updater."""
    global _updater_task
    stop_updater()
    _updater_task = asyncio.ensure_future(_updater_loop(subdomain, token))
    logger.info("DuckDNS updater started for %s.duckdns.org", subdomain)


def stop_updater():
    """Stop the periodic IP updater."""
    global _updater_task
    if _updater_task:
        _updater_task.cancel()
        _updater_task = None


# ── Let's Encrypt certificate ────────────────────────────────────────────────

def _write_hook_scripts(subdomain: str, token: str):
    """Write certbot DNS-01 auth/cleanup hook scripts for DuckDNS."""
    hook_dir = Path("/data/certbot-hooks")
    hook_dir.mkdir(parents=True, exist_ok=True)
    auth = hook_dir / "auth.sh"
    cleanup = hook_dir / "cleanup.sh"

    auth.write_text(
        "#!/bin/sh\n"
        f'curl -s "{DUCKDNS_UPDATE_URL}?domains={subdomain}&token={token}'
        '&txt=$CERTBOT_VALIDATION"\n'
        "sleep 30\n"
    )
    cleanup.write_text(
        "#!/bin/sh\n"
        f'curl -s "{DUCKDNS_UPDATE_URL}?domains={subdomain}&token={token}'
        '&txt=&clear=true"\n'
    )
    os.chmod(auth, 0o700)
    os.chmod(cleanup, 0o700)
    return str(auth), str(cleanup)


async def obtain_cert(subdomain: str, token: str) -> dict:
    """Obtain/renew a Let's Encrypt cert via DNS-01 challenge with DuckDNS.

    Returns {success, error?}.
    """
    domain = f"{subdomain}.duckdns.org"
    CERT_DIR.mkdir(parents=True, exist_ok=True)

    auth_hook, cleanup_hook = _write_hook_scripts(subdomain, token)

    # Use certonly — always tries to get/renew
    cmd = [
        "certbot", "certonly",
        "--non-interactive",
        "--agree-tos",
        "--register-unsafely-without-email",
        "--preferred-challenges", "dns",
        "--manual",
        "--manual-auth-hook", auth_hook,
        "--manual-cleanup-hook", cleanup_hook,
        "-d", domain,
        "--config-dir", str(CERT_DIR / "config"),
        "--work-dir", str(CERT_DIR / "work"),
        "--logs-dir", str(CERT_DIR / "logs"),
    ]

    # If cert already exists, add --keep to only renew if needed
    live_dir = CERT_DIR / "config" / "live" / domain
    if (live_dir / "fullchain.pem").exists():
        cmd.append("--keep-until-expiring")

    logger.info("Requesting Let's Encrypt certificate for %s ...", domain)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip() or stdout.decode().strip()
        logger.error("certbot failed (exit %d): %s", proc.returncode, err)
        return {"success": False, "error": f"certbot failed: {err[:200]}"}

    # Copy certs to our known paths
    if (live_dir / "fullchain.pem").exists():
        shutil.copy2(live_dir / "fullchain.pem", CERT_PATH)
        shutil.copy2(live_dir / "privkey.pem", KEY_PATH)
        os.chmod(str(KEY_PATH), 0o600)
        logger.info("Certificate ready for %s", domain)
        return {"success": True}

    return {"success": False, "error": "Certificate files not found after certbot"}


def has_cert() -> bool:
    """Check if a valid TLS certificate exists."""
    return CERT_PATH.exists() and KEY_PATH.exists()


async def _renewal_loop(subdomain: str, token: str):
    """Background loop checking cert renewal every 12 hours."""
    while True:
        await asyncio.sleep(43200)  # 12 hours
        logger.info("Checking certificate renewal...")
        result = await obtain_cert(subdomain, token)
        if result["success"]:
            # Restart HTTPS server with new cert
            await stop_https_server()
            await start_https_server()


def start_renewal_checker(subdomain: str, token: str):
    """Start periodic cert renewal checker."""
    global _renewal_task
    stop_renewal_checker()
    _renewal_task = asyncio.ensure_future(_renewal_loop(subdomain, token))


def stop_renewal_checker():
    """Stop the renewal checker."""
    global _renewal_task
    if _renewal_task:
        _renewal_task.cancel()
        _renewal_task = None


# ── HTTPS server ─────────────────────────────────────────────────────────────

async def _serve_public_key(request):
    """Serve the public key PEM file."""
    if PUBLIC_KEY_PATH.exists():
        return web.Response(
            body=PUBLIC_KEY_PATH.read_bytes(),
            content_type="application/x-pem-file",
        )
    return web.Response(status=404)


async def start_https_server() -> bool:
    """Start HTTPS server on port 443 serving the public key."""
    global _https_runner

    if not has_cert():
        logger.error("Cannot start HTTPS server — no TLS certificate")
        return False

    if not PUBLIC_KEY_PATH.exists():
        logger.error("Cannot start HTTPS server — no public key")
        return False

    # Stop existing if running
    await stop_https_server()

    app = web.Application()
    app.router.add_get(
        "/.well-known/appspecific/com.tesla.3p.public-key.pem",
        _serve_public_key,
    )

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(str(CERT_PATH), str(KEY_PATH))

    _https_runner = web.AppRunner(app)
    await _https_runner.setup()
    site = web.TCPSite(_https_runner, "0.0.0.0", HTTPS_PORT, ssl_context=ssl_ctx)
    await site.start()
    logger.info("HTTPS server started on port %d (serving .well-known)", HTTPS_PORT)
    return True


async def stop_https_server():
    """Stop the HTTPS server."""
    global _https_runner
    if _https_runner:
        await _https_runner.cleanup()
        _https_runner = None


def https_running() -> bool:
    """Check if the HTTPS server is running."""
    return _https_runner is not None


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def start_all(subdomain: str, token: str):
    """Start IP updater, HTTPS server, and renewal checker."""
    start_updater(subdomain, token)
    await start_https_server()
    start_renewal_checker(subdomain, token)


async def stop_all():
    """Stop everything."""
    stop_updater()
    stop_renewal_checker()
    await stop_https_server()
