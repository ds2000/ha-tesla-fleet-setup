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


async def get_ha_info() -> dict:
    """Gather HA environment info for the wizard."""
    result = {
        "external_url": None,
        "has_nabu_casa": False,
        "has_external_url": False,
    }

    # Method 1: Check Nabu Casa (HA Cloud) via cloud status
    cloud_info = await _supervisor_get("/core/api/cloud/status")
    if cloud_info:
        logger.info("Cloud status: logged_in=%s, remote_connected=%s, remote_domain=%s",
                     cloud_info.get("logged_in"), cloud_info.get("remote_connected"),
                     cloud_info.get("remote_domain"))
        if cloud_info.get("remote_connected") and cloud_info.get("remote_domain"):
            url = f"https://{cloud_info['remote_domain']}"
            logger.info("Detected Nabu Casa URL: %s", url)
            result["external_url"] = url
            result["has_nabu_casa"] = True
            result["has_external_url"] = True
            return result
    else:
        logger.warning("Cloud status endpoint returned no data")

    # Method 2: Check HA core config for external_url
    core_info = await _supervisor_get("/core/api/config")
    if core_info:
        external_url = core_info.get("external_url")
        if external_url:
            url = external_url.rstrip("/")
            logger.info("Detected external URL from config: %s", url)
            result["external_url"] = url
            result["has_nabu_casa"] = "nabu.casa" in url
            result["has_external_url"] = True
        else:
            logger.info("No external_url configured in HA settings")
    else:
        logger.warning("Core config endpoint returned no data")

    return result
