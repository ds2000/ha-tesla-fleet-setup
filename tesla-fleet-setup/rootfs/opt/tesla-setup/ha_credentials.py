"""Inject Application Credentials into Home Assistant via WebSocket API.

Connects to HA Core through the Supervisor WebSocket proxy and creates
Application Credentials so the Tesla Fleet integration picks them up
automatically — no manual re-entry needed.
"""

import asyncio
import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

SUPERVISOR_WS = "ws://supervisor/core/websocket"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


async def inject_credentials(client_id: str, client_secret: str) -> dict:
    """Create Application Credentials for tesla_fleet in HA.

    Returns {"success": True/False, "error": str|None, "already_exists": bool}.
    """
    if not SUPERVISOR_TOKEN:
        logger.warning("No SUPERVISOR_TOKEN — cannot inject credentials (running outside HA?)")
        return {"success": False, "error": "No SUPERVISOR_TOKEN available"}

    try:
        return await _ws_inject(client_id, client_secret)
    except Exception as e:
        logger.error("Failed to inject credentials: %s", e)
        return {"success": False, "error": str(e)}


async def _ws_inject(client_id: str, client_secret: str) -> dict:
    """Connect to HA WebSocket and create the credential."""
    msg_id = 1

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            SUPERVISOR_WS,
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=aiohttp.ClientWSTimeout(ws_close=10),
        ) as ws:
            # Step 1: Receive auth_required
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if msg.get("type") != "auth_required":
                return {"success": False, "error": f"Unexpected WS message: {msg.get('type')}"}

            # Step 2: Send auth
            await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if msg.get("type") != "auth_ok":
                return {"success": False, "error": f"WS auth failed: {msg.get('message', 'unknown')}"}

            logger.debug("WebSocket authenticated with HA Core")

            # Step 3: Check if credential already exists
            msg_id += 1
            await ws.send_json({"id": msg_id, "type": "application_credentials/list"})
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)

            if msg.get("success"):
                existing = msg.get("result", [])
                for cred in existing:
                    if cred.get("domain") == "tesla_fleet" and cred.get("client_id") == client_id:
                        logger.info("Application Credentials already exist for tesla_fleet")
                        return {"success": True, "already_exists": True}

            # Step 4: Create the credential
            msg_id += 1
            await ws.send_json({
                "id": msg_id,
                "type": "application_credentials/create",
                "domain": "tesla_fleet",
                "client_id": client_id,
                "client_secret": client_secret,
                "name": "Tesla Fleet (via setup wizard)",
            })
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)

            if msg.get("success"):
                logger.info("Application Credentials injected for tesla_fleet")
                return {"success": True, "already_exists": False}

            error = msg.get("error", {})
            error_msg = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
            logger.error("Failed to create credentials: %s", error_msg)
            return {"success": False, "error": error_msg}
