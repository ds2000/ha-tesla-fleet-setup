"""Wrapper around Tesla's tesla-control CLI for signed vehicle commands."""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

TESLA_CONTROL = "/usr/local/bin/tesla-control"
PRIVATE_KEY_PATH = Path("/data/keys/private.pem")
TOKEN_PATH = Path("/data/tesla-token.json")
CACHE_PATH = Path("/data/tesla-session-cache.json")


def _write_token_file(access_token: str):
    """Write OAuth token to a file for tesla-control to read."""
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"access_token": access_token}))
    os.replace(tmp, TOKEN_PATH)
    os.chmod(TOKEN_PATH, 0o600)


async def run_command(access_token: str, vin: str, command: str,
                      args: list[str] | None = None, timeout: float = 30) -> dict:
    """Run a tesla-control command and return the result.

    Args:
        access_token: OAuth access token
        vin: Vehicle Identification Number
        command: tesla-control command name (e.g. "lock", "flash-lights")
        args: Additional command arguments
        timeout: Timeout in seconds

    Returns:
        {"success": True/False, "output": str, "error": str}
    """
    if not Path(TESLA_CONTROL).exists():
        return {"success": False, "error": "tesla-control binary not found"}
    if not PRIVATE_KEY_PATH.exists():
        return {"success": False, "error": "Private key not found"}

    _write_token_file(access_token)

    cmd = [
        TESLA_CONTROL,
        "-vin", vin,
        "-key-file", str(PRIVATE_KEY_PATH),
        "-token-file", str(TOKEN_PATH),
        "-session-cache", str(CACHE_PATH),
        command,
    ]
    if args:
        cmd.extend(args)

    logger.info("Running tesla-control: %s %s", command, " ".join(args or []))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        stdout_text = stdout.decode().strip()
        stderr_text = stderr.decode().strip()

        if proc.returncode == 0:
            logger.info("tesla-control %s succeeded", command)
            return {"success": True, "output": stdout_text or "OK"}
        else:
            logger.error("tesla-control %s failed (exit %d): %s",
                         command, proc.returncode, stderr_text)
            return {"success": False, "error": stderr_text or f"Exit code {proc.returncode}"}

    except asyncio.TimeoutError:
        logger.error("tesla-control %s timed out after %ds", command, timeout)
        return {"success": False, "error": f"Command timed out after {timeout}s"}
    except Exception as e:
        logger.error("tesla-control %s error: %s", command, e)
        return {"success": False, "error": str(e)}


async def add_key_request(access_token: str, vin: str) -> dict:
    """Send a key pairing request to the vehicle.

    The user must then tap their NFC key card on the center console to approve.
    The vehicle must be awake and nearby is not required (works over internet).
    """
    return await run_command(
        access_token, vin, "add-key-request",
        args=[str(PRIVATE_KEY_PATH), "owner", "cloud_key"],
        timeout=30,
    )


async def signed_command(access_token: str, vin: str, command: str,
                         args: list[str] | None = None) -> dict:
    """Send a signed command to the vehicle via tesla-control."""
    return await run_command(access_token, vin, command, args=args, timeout=15)


def is_available() -> bool:
    """Check if tesla-control binary is available."""
    return Path(TESLA_CONTROL).exists()
