"""Detect Home Assistant environment: Nabu Casa, external URL, etc."""

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

SUPERVISOR_API = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


async def _supervisor_get(path: str) -> dict | None:
    """Make a GET request to the HA Supervisor API."""
    if not SUPERVISOR_TOKEN:
        logger.warning("No SUPERVISOR_TOKEN available")
        return None
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{SUPERVISOR_API}{path}", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", data)
    except Exception as e:
        logger.error("Supervisor API call failed for %s: %s", path, e)
    return None


async def get_external_url() -> str | None:
    """Try to detect the external URL for this HA instance."""
    # Method 1: Check Nabu Casa (HA Cloud)
    cloud_info = await _supervisor_get("/core/api/cloud/status")
    if cloud_info:
        remote = cloud_info.get("remote_connected", False)
        if remote:
            instance = cloud_info.get("cloud", {})
            # Nabu Casa remote URL pattern
            prefs = cloud_info.get("prefs", {})
            remote_domain = cloud_info.get("remote_domain")
            if remote_domain:
                url = f"https://{remote_domain}"
                logger.info("Detected Nabu Casa URL: %s", url)
                return url

    # Method 2: Check HA core config for external_url
    core_info = await _supervisor_get("/core/api/config")
    if core_info:
        external_url = core_info.get("external_url")
        if external_url:
            logger.info("Detected configured external URL: %s", external_url)
            return external_url.rstrip("/")

    return None


async def get_ha_info() -> dict:
    """Gather HA environment info for the wizard."""
    external_url = await get_external_url()
    return {
        "external_url": external_url,
        "has_nabu_casa": external_url and "nabu.casa" in external_url,
        "has_external_url": external_url is not None,
    }
