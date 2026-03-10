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
import socket
import ssl
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

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


# ── UPnP port forwarding ─────────────────────────────────────────────────────

def _find_lan_ip() -> str | None:
    """Find the LAN IP address (non-loopback, non-docker) for UPnP binding."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _ssdp_discover(lan_ip: str | None, timeout: float = 5.0) -> str | None:
    """Send SSDP M-SEARCH and return the first IGD location URL found."""
    ssdp_msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\n"
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
        "\r\n"
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    try:
        if lan_ip:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(lan_ip))
            sock.bind((lan_ip, 0))

        sock.sendto(ssdp_msg.encode(), ("239.255.255.250", 1900))
        while True:
            try:
                data, _ = sock.recvfrom(4096)
                response = data.decode(errors="replace")
                headers = {}
                for line in response.splitlines():
                    if ":" in line:
                        key, val = line.split(":", 1)
                        headers[key.strip().lower()] = val.strip()
                # Only accept responses that are actually IGD devices
                st = headers.get("st", "")
                if "internetgatewaydevice" not in st.lower():
                    logger.debug("UPnP: skipping non-IGD response (ST=%s)", st)
                    continue
                if "location" in headers:
                    return headers["location"]
            except socket.timeout:
                break
    finally:
        sock.close()
    return None


async def _find_control_url(location: str) -> str | None:
    """Fetch IGD root description XML and find the WANIPConnection control URL."""
    try:
        async with aiohttp.ClientSession() as session, session.get(location, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            xml_text = await resp.text()

        root = ElementTree.fromstring(xml_text)  # noqa: S314 — trusted local network device

        # Search for WANIPConnection or WANPPPConnection service
        for service in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
            stype = service.findtext("{urn:schemas-upnp-org:device-1-0}serviceType", "")
            if "WANIPConnection" in stype or "WANPPPConnection" in stype:
                control = service.findtext("{urn:schemas-upnp-org:device-1-0}controlURL", "")
                if control:
                    parsed = urlparse(location)
                    base = f"{parsed.scheme}://{parsed.netloc}"
                    if control.startswith("/"):
                        return base + control
                    return base + "/" + control
    except Exception as e:
        logger.warning("UPnP: failed to parse root description: %s", e)
    return None


async def _soap_add_port_mapping(control_url: str, port: int, lan_ip: str,
                                  ext_port: int | None = None, description: str = "HA Tesla Fleet") -> dict:
    """Call AddPortMapping via UPnP SOAP action."""
    if ext_port is None:
        ext_port = port
    # Try both WANIPConnection and WANPPPConnection service types
    for service_type in [
        "urn:schemas-upnp-org:service:WANIPConnection:1",
        "urn:schemas-upnp-org:service:WANPPPConnection:1",
    ]:
        soap_body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<u:AddPortMapping xmlns:u="{service_type}">'
            "<NewRemoteHost></NewRemoteHost>"
            f"<NewExternalPort>{ext_port}</NewExternalPort>"
            "<NewProtocol>TCP</NewProtocol>"
            f"<NewInternalPort>{port}</NewInternalPort>"
            f"<NewInternalClient>{lan_ip}</NewInternalClient>"
            "<NewEnabled>1</NewEnabled>"
            f"<NewPortMappingDescription>{description}</NewPortMappingDescription>"
            "<NewLeaseDuration>3600</NewLeaseDuration>"
            "</u:AddPortMapping>"
            "</s:Body>"
            "</s:Envelope>"
        )
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{service_type}#AddPortMapping"',
        }
        try:
            async with aiohttp.ClientSession() as session, session.post(
                control_url, data=soap_body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.text()
                if resp.status == 200 or "<errorCode>718</errorCode>" in body:
                    # 718 = ConflictInMappingEntry (already mapped) — that's fine
                    logger.info("UPnP: port %d mapped via %s", port, service_type)
                    return {"success": True}
                logger.debug("UPnP SOAP response (%d): %s", resp.status, body[:200])
        except Exception as e:
            logger.debug("UPnP SOAP failed for %s: %s", service_type, e)

    return {"success": False, "error": "SOAP AddPortMapping failed on all service types"}


async def open_port_upnp(port: int = HTTPS_PORT, ext_port: int | None = None) -> dict:
    """Try to open a port on the router via UPnP IGD. Uses pure Python SSDP + SOAP."""
    try:
        lan_ip = _find_lan_ip()
        logger.info("UPnP: detected LAN IP: %s", lan_ip)
        if not lan_ip:
            return {"success": False, "error": "Could not detect LAN IP address"}

        # SSDP discovery — find IGD device
        location = await asyncio.get_event_loop().run_in_executor(
            None, _ssdp_discover, lan_ip, 5.0
        )
        if not location:
            logger.warning("UPnP: no IGD device found via SSDP")
            return {"success": False, "error": "No UPnP router found on the network"}

        logger.info("UPnP: found IGD at %s", location)

        # Parse the root description XML to find the control URL
        control_url = await _find_control_url(location)
        if not control_url:
            logger.warning("UPnP: could not find WANIPConnection control URL in %s", location)
            return {"success": False, "error": f"Found router at {location} but no port mapping service"}

        logger.info("UPnP: control URL: %s", control_url)

        # Add the port mapping via SOAP
        result = await _soap_add_port_mapping(control_url, port, lan_ip, ext_port=ext_port)
        ep = ext_port or port
        if result["success"]:
            logger.info("UPnP: ext %d → int %d forwarded via %s", ep, port, control_url)
        return result

    except Exception as e:
        logger.warning("UPnP error: %s", e)
        return {"success": False, "error": str(e)}


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
    """Background loop updating DuckDNS IP every 5 minutes.

    Also refreshes the UPnP port mapping every cycle (mappings can expire).
    """
    while True:
        await update_ip(subdomain, token)
        # Silently refresh UPnP mapping (they expire on many routers)
        await open_port_upnp()
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
    """Start IP updater, HTTPS server, UPnP port forward, and renewal checker."""
    # Try UPnP port forward (best effort — won't block if router doesn't support it)
    upnp = await open_port_upnp()
    if upnp["success"]:
        logger.info("UPnP: port %d forwarded on startup", HTTPS_PORT)
    else:
        logger.info("UPnP unavailable on startup: %s (manual port forward needed)", upnp.get("error", "unknown"))
    start_updater(subdomain, token)
    await start_https_server()
    start_renewal_checker(subdomain, token)


async def stop_all():
    """Stop everything."""
    stop_updater()
    stop_renewal_checker()
    await stop_https_server()
