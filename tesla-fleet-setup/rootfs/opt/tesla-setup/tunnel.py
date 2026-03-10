"""Cloudflare named tunnel management.

Runs `cloudflared tunnel run --token <TOKEN>` as a persistent subprocess.
Named tunnels have stable hostnames that don't change on restart — unlike
quick tunnels which generated random trycloudflare.com URLs.

Used as a fallback when DuckDNS port 443 isn't reachable (ISP blocking,
double NAT, etc.).
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_process: asyncio.subprocess.Process | None = None
_token: str | None = None
_domain: str | None = None
_restart_task: asyncio.Task | None = None


def is_running() -> bool:
    return _process is not None and _process.returncode is None


def get_domain() -> str | None:
    return _domain


async def start(token: str, domain: str) -> dict:
    """Start a named Cloudflare tunnel. Returns {success, error?}."""
    global _process, _token, _domain, _restart_task

    if is_running():
        return {"success": True, "domain": _domain}

    _token = token
    _domain = domain

    try:
        # Log token length/format for debugging (never log the actual token)
        logger.info("Starting cloudflared with token length=%d", len(token))

        _process = await asyncio.create_subprocess_exec(
            "cloudflared", "--no-autoupdate", "tunnel", "run", "--token", token,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        # Give it time to connect
        await asyncio.sleep(8)

        if _process.returncode is not None:
            output = await _process.stdout.read()
            error = output.decode(errors="replace").strip()[-800:]
            logger.error("Cloudflare tunnel exited (code %d): %s", _process.returncode, error)
            _process = None
            return {"success": False, "error": error or f"Tunnel exited with code {_process.returncode}"}

        logger.info("Cloudflare tunnel started for %s (pid %d)", domain, _process.pid)

        # Start auto-restart watcher
        if _restart_task is None or _restart_task.done():
            _restart_task = asyncio.create_task(_watch_and_restart())

        return {"success": True, "domain": domain}

    except FileNotFoundError:
        return {"success": False, "error": "cloudflared not installed"}
    except Exception as e:
        logger.error("Failed to start Cloudflare tunnel: %s", e)
        return {"success": False, "error": str(e)}


async def stop():
    """Stop the tunnel."""
    global _process, _restart_task
    if _restart_task and not _restart_task.done():
        _restart_task.cancel()
        _restart_task = None
    if _process:
        logger.info("Stopping Cloudflare tunnel")
        try:
            _process.terminate()
            await asyncio.wait_for(_process.wait(), timeout=10)
        except (asyncio.TimeoutError, ProcessLookupError):
            _process.kill()
        _process = None


async def _watch_and_restart():
    """Watch the tunnel process and restart if it dies."""
    global _process
    while _token and _domain:
        if _process:
            await _process.wait()
            logger.warning("Cloudflare tunnel exited (code %d), restarting in 5s...", _process.returncode)
            _process = None
        await asyncio.sleep(5)
        if _token and _domain:
            try:
                _process = await asyncio.create_subprocess_exec(
                    "cloudflared", "--no-autoupdate", "tunnel", "run", "--token", _token,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                logger.info("Cloudflare tunnel restarted (pid %d)", _process.pid)
            except Exception as e:
                logger.error("Failed to restart tunnel: %s", e)
                await asyncio.sleep(30)


def status() -> dict:
    """Get tunnel status."""
    return {
        "running": is_running(),
        "domain": _domain,
        "method": "cloudflare",
    }
