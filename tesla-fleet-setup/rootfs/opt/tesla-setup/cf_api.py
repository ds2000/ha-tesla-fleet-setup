"""Cloudflare API client for automated tunnel setup.

Automates:
  1. Fetch account ID and zones (domains)
  2. Create a named tunnel
  3. Configure tunnel ingress (route domain → localhost:8099)
  4. Create DNS CNAME record pointing to the tunnel
  5. Return tunnel token for cloudflared

Requires a Cloudflare API token with permissions:
  - Account > Cloudflare Tunnel > Edit
  - Zone > DNS > Edit
"""

import logging
import secrets

import aiohttp

logger = logging.getLogger(__name__)

CF_API = "https://api.cloudflare.com/client/v4"


class CloudflareAPI:
    def __init__(self, api_token: str):
        self._token = api_token
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    async def _get(self, path: str) -> dict:
        async with aiohttp.ClientSession() as s, s.get(
            f"{CF_API}{path}", headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            return await r.json()

    async def _post(self, path: str, json: dict | None = None) -> dict:
        async with aiohttp.ClientSession() as s, s.post(
            f"{CF_API}{path}", headers=self._headers, json=json or {},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            return await r.json()

    async def _put(self, path: str, json: dict | None = None) -> dict:
        async with aiohttp.ClientSession() as s, s.put(
            f"{CF_API}{path}", headers=self._headers, json=json or {},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            return await r.json()

    async def verify_token(self) -> dict:
        """Verify the API token is valid. Returns {success, error?}."""
        data = await self._get("/user/tokens/verify")
        if data.get("success"):
            return {"success": True}
        errors = data.get("errors", [{}])
        return {"success": False, "error": errors[0].get("message", "Invalid token")}

    async def get_account_id(self) -> str | None:
        """Get the first account ID."""
        data = await self._get("/accounts?per_page=1")
        results = data.get("result", [])
        if results:
            return results[0]["id"]
        return None

    async def get_zones(self) -> list[dict]:
        """Get list of zones (domains) on the account."""
        data = await self._get("/zones?per_page=50&status=active")
        return [{"id": z["id"], "name": z["name"]} for z in data.get("result", [])]

    async def create_tunnel(self, account_id: str, name: str) -> dict:
        """Create a named tunnel. Returns {id, token} or {error}."""
        tunnel_secret = secrets.token_urlsafe(32)
        data = await self._post(f"/accounts/{account_id}/cfd_tunnel", json={
            "name": name,
            "tunnel_secret": tunnel_secret,
            "config_src": "cloudflare",
        })
        if not data.get("success"):
            errors = data.get("errors", [{}])
            msg = errors[0].get("message", "Failed to create tunnel")
            # If tunnel already exists, try to find it
            if "already exists" in msg.lower() or errors[0].get("code") == 52007:
                existing = await self._find_tunnel(account_id, name)
                if existing:
                    return existing
            return {"error": msg}

        tunnel = data["result"]
        tunnel_id = tunnel["id"]
        # Get the tunnel token
        token_data = await self._get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
        token = token_data.get("result", "")

        return {"id": tunnel_id, "token": token}

    async def _find_tunnel(self, account_id: str, name: str) -> dict | None:
        """Find an existing tunnel by name."""
        data = await self._get(f"/accounts/{account_id}/cfd_tunnel?name={name}&is_deleted=false")
        tunnels = data.get("result", [])
        if not tunnels:
            return None
        tunnel_id = tunnels[0]["id"]
        token_data = await self._get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
        token = token_data.get("result", "")
        return {"id": tunnel_id, "token": token}

    async def configure_tunnel(self, account_id: str, tunnel_id: str,
                                hostname: str, service: str = "http://localhost:8099") -> dict:
        """Set tunnel ingress configuration."""
        data = await self._put(
            f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
            json={
                "config": {
                    "ingress": [
                        {"hostname": hostname, "service": service},
                        {"service": "http_status:404"},  # catch-all
                    ]
                }
            },
        )
        if data.get("success"):
            return {"success": True}
        errors = data.get("errors", [{}])
        return {"success": False, "error": errors[0].get("message", "Failed to configure tunnel")}

    async def create_dns_record(self, zone_id: str, subdomain: str,
                                 tunnel_id: str, zone_name: str) -> dict:
        """Create a CNAME record pointing subdomain to the tunnel."""
        full_name = f"{subdomain}.{zone_name}" if subdomain else zone_name
        # Check if record already exists
        existing = await self._get(f"/zones/{zone_id}/dns_records?name={full_name}&type=CNAME")
        if existing.get("result"):
            # Update existing record
            record_id = existing["result"][0]["id"]
            data = await self._put(f"/zones/{zone_id}/dns_records/{record_id}", json={
                "type": "CNAME",
                "name": full_name,
                "content": f"{tunnel_id}.cfargotunnel.com",
                "proxied": True,
            })
        else:
            data = await self._post(f"/zones/{zone_id}/dns_records", json={
                "type": "CNAME",
                "name": full_name,
                "content": f"{tunnel_id}.cfargotunnel.com",
                "proxied": True,
            })
        if data.get("success"):
            return {"success": True, "hostname": full_name}
        errors = data.get("errors", [{}])
        return {"success": False, "error": errors[0].get("message", "Failed to create DNS record")}


async def auto_setup(api_token: str, zone_id: str, zone_name: str,
                      subdomain: str = "tesla", tunnel_name: str = "ha-tesla-fleet") -> dict:
    """Fully automated Cloudflare tunnel setup.

    Returns {success, tunnel_token, domain} or {success: False, error, step}.
    """
    cf = CloudflareAPI(api_token)

    # 1. Verify token
    verify = await cf.verify_token()
    if not verify["success"]:
        return {"success": False, "error": verify["error"], "step": "verify"}

    # 2. Get account ID
    account_id = await cf.get_account_id()
    if not account_id:
        return {"success": False, "error": "No Cloudflare account found", "step": "account"}

    # 3. Create tunnel
    tunnel = await cf.create_tunnel(account_id, tunnel_name)
    if "error" in tunnel:
        return {"success": False, "error": tunnel["error"], "step": "tunnel"}

    tunnel_id = tunnel["id"]
    tunnel_token = tunnel["token"]
    hostname = f"{subdomain}.{zone_name}" if subdomain else zone_name

    # 4. Configure tunnel ingress
    config = await cf.configure_tunnel(account_id, tunnel_id, hostname)
    if not config["success"]:
        return {"success": False, "error": config["error"], "step": "config"}

    # 5. Create DNS record
    dns = await cf.create_dns_record(zone_id, subdomain, tunnel_id, zone_name)
    if not dns["success"]:
        return {"success": False, "error": dns["error"], "step": "dns"}

    logger.info("Cloudflare tunnel setup complete: %s → tunnel %s", hostname, tunnel_id)
    return {
        "success": True,
        "tunnel_token": tunnel_token,
        "tunnel_id": tunnel_id,
        "domain": hostname,
    }
