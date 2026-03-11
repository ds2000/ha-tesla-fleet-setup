"""Cloudflare API client for automated tunnel setup.

Automates:
  1. Fetch account ID and zones (domains)
  2. Create a named tunnel
  3. Configure tunnel ingress (route domain -> localhost:8099)
  4. Create DNS CNAME record pointing to the tunnel
  5. Return tunnel token for cloudflared

Requires a Cloudflare API token with permissions:
  - Account > Cloudflare Tunnel > Edit
  - Zone > Zone > Read
  - Zone > DNS > Edit
"""

import base64
import logging
import os
from urllib.parse import quote

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

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        """Make an authenticated request to the Cloudflare API."""
        async with aiohttp.ClientSession() as s:
            async with s.request(
                method, f"{CF_API}{path}", headers=self._headers, json=json,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.content_type != "application/json":
                    text = await r.text()
                    logger.error("Cloudflare API returned non-JSON (HTTP %d): %s", r.status, text[:200])
                    return {"success": False, "errors": [{"message": f"HTTP {r.status}: non-JSON response"}]}
                return await r.json()

    async def _get(self, path: str) -> dict:
        return await self._request("GET", path)

    async def _post(self, path: str, json: dict | None = None) -> dict:
        return await self._request("POST", path, json=json or {})

    async def _put(self, path: str, json: dict | None = None) -> dict:
        return await self._request("PUT", path, json=json or {})

    async def verify_token(self) -> dict:
        """Verify the API token is valid. Returns {success, error?}."""
        data = await self._get("/user/tokens/verify")
        if data.get("success"):
            return {"success": True}
        errors = data.get("errors", [{}])
        return {"success": False, "error": errors[0].get("message", "Invalid token")}

    async def get_account_id(self) -> str | None:
        """Get the first account ID (tries /accounts, then falls back to zone data)."""
        data = await self._get("/accounts?per_page=1")
        results = data.get("result", [])
        if results:
            return results[0]["id"]
        return None

    async def get_zones(self) -> list[dict]:
        """Get list of zones (domains) on the account. Also extracts account_id."""
        data = await self._get("/zones?per_page=50&status=active")
        zones = []
        for z in data.get("result", []):
            zone = {"id": z["id"], "name": z["name"]}
            if z.get("account", {}).get("id"):
                zone["account_id"] = z["account"]["id"]
            zones.append(zone)
        return zones

    async def create_or_reuse_tunnel(self, account_id: str, name: str) -> dict:
        """Create a named tunnel, or reuse existing one. Returns {id, token} or {error}."""
        existing = await self._find_tunnel(account_id, name)
        if existing:
            logger.info("Reusing existing tunnel '%s' (%s)", name, existing["id"])
            return existing

        tunnel_secret = base64.b64encode(os.urandom(32)).decode()
        data = await self._post(f"/accounts/{account_id}/cfd_tunnel", json={
            "name": name,
            "tunnel_secret": tunnel_secret,
            "config_src": "cloudflare",
        })
        if not data.get("success"):
            errors = data.get("errors", [{}])
            msg = errors[0].get("message", "Failed to create tunnel")
            return {"error": msg}

        result = data["result"]
        tunnel_id = result["id"]
        token_data = await self._get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
        token = token_data.get("result", "")
        logger.info("Created new tunnel '%s' (%s)", name, tunnel_id)

        return {"id": tunnel_id, "token": token}

    async def _find_tunnel(self, account_id: str, name: str) -> dict | None:
        """Find an existing tunnel by name."""
        safe_name = quote(name, safe="")
        data = await self._get(f"/accounts/{account_id}/cfd_tunnel?name={safe_name}&is_deleted=false")
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
                        {"service": "http_status:404"},
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
        safe_name = quote(full_name, safe="")
        existing = await self._get(f"/zones/{zone_id}/dns_records?name={safe_name}&type=CNAME")
        if existing.get("result"):
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
