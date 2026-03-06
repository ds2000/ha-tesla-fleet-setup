"""Detect Home Assistant environment: Nabu Casa, external URL, etc."""

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

SUPERVISOR_API = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Cloud API paths to try (varies by HA version)
CLOUD_PATHS = [
    "/core/api/cloud/status",
    "/core/api/cloud",
]


async def _supervisor_get(path: str) -> dict | None:
    """Make a GET request to the HA Supervisor API."""
    if not SUPERVISOR_TOKEN:
        logger.warning("No SUPERVISOR_TOKEN available — running outside HA?")
        return None
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{SUPERVISOR_API}{path}", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.debug("Supervisor %s response: %s", path, data)
                    return data.get("data", data)
                else:
                    body = await resp.text()
                    logger.warning("Supervisor %s returned %d: %s", path, resp.status, body[:200])
    except Exception as e:
        logger.error("Supervisor API call failed for %s: %s", path, e)
    return None


def _extract_nabu_casa_url(cloud_info: dict) -> str | None:
    """Try to extract the Nabu Casa remote URL from cloud info response."""
    # Check various known response formats
    remote_domain = cloud_info.get("remote_domain")
    if not remote_domain:
        # Some versions nest it under "cloud" or "prefs"
        for key in ("cloud", "prefs", "remote"):
            sub = cloud_info.get(key)
            if isinstance(sub, dict):
                remote_domain = sub.get("remote_domain") or sub.get("domain")
                if remote_domain:
                    break

    if remote_domain:
        # Check that remote is actually connected/enabled
        remote_ok = (
            cloud_info.get("remote_connected", False)
            or cloud_info.get("remote_enabled", False)
            or cloud_info.get("logged_in", False)
        )
        if remote_ok:
            return f"https://{remote_domain}"
        else:
            logger.info("Nabu Casa domain found (%s) but remote not connected", remote_domain)

    return None


async def get_ha_info() -> dict:
    """Gather HA environment info for the wizard."""
    result = {
        "external_url": None,
        "has_nabu_casa": False,
        "has_external_url": False,
    }

    # Method 1: Try cloud status endpoints (path varies by HA version)
    for path in CLOUD_PATHS:
        cloud_info = await _supervisor_get(path)
        if cloud_info:
            logger.info("Cloud info from %s: keys=%s", path, list(cloud_info.keys()))
            url = _extract_nabu_casa_url(cloud_info)
            if url:
                logger.info("Detected Nabu Casa URL: %s", url)
                result["external_url"] = url
                result["has_nabu_casa"] = True
                result["has_external_url"] = True
                return result

    # Method 2: Check HA core config for external_url and cloud component
    core_info = await _supervisor_get("/core/api/config")
    if core_info:
        # Check external_url
        external_url = core_info.get("external_url")
        if external_url:
            url = external_url.rstrip("/")
            logger.info("Detected external URL from config: %s", url)
            result["external_url"] = url
            result["has_nabu_casa"] = "nabu.casa" in url
            result["has_external_url"] = True
            return result

        # Check if cloud component is loaded (Nabu Casa installed but URL not found)
        components = core_info.get("components", [])
        if "cloud" in components:
            logger.info("Cloud component loaded but could not detect remote URL. "
                        "User may need to enter URL manually or set external_url in HA settings.")
            result["has_nabu_casa"] = True  # Signal that cloud exists
        else:
            logger.info("No cloud component loaded and no external_url configured")
    else:
        logger.warning("Core config endpoint returned no data")

    return result
