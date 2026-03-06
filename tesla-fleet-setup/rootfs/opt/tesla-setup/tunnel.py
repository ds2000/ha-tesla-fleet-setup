"""Cloudflare quick tunnel management."""

import asyncio
import logging
import re

logger = logging.getLogger(__name__)


class TunnelManager:
    """Manages a cloudflared quick tunnel subprocess."""

    def __init__(self):
        self._process: asyncio.subprocess.Process | None = None
        self._url: str | None = None

    @property
    def url(self) -> str | None:
        return self._url

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, local_port: int) -> str:
        """Start a quick tunnel pointing to local_port. Returns the public URL."""
        if self.running:
            return self._url

        logger.info("Starting Cloudflare quick tunnel -> localhost:%d", local_port)

        self._process = await asyncio.create_subprocess_exec(
            "cloudflared", "tunnel", "--url", f"http://localhost:{local_port}",
            "--no-autoupdate",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # cloudflared prints the URL to stderr
        url = await self._read_url(self._process.stderr)
        if not url:
            await self.stop()
            raise RuntimeError("Failed to get tunnel URL from cloudflared")

        self._url = url
        logger.info("Tunnel active at %s", self._url)
        return self._url

    async def _read_url(self, stream, timeout: float = 30.0) -> str | None:
        """Read cloudflared stderr until we find the tunnel URL."""
        pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            buffer = ""
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    chunk = await asyncio.wait_for(stream.read(1024), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                text = chunk.decode(errors="replace")
                buffer += text
                logger.debug("cloudflared: %s", text.strip())
                match = pattern.search(buffer)
                if match:
                    return match.group(0)
        except Exception as e:
            logger.error("Error reading tunnel URL: %s", e)
        return None

    async def stop(self):
        """Stop the tunnel."""
        if self._process:
            logger.info("Stopping Cloudflare tunnel")
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._process.kill()
            self._process = None
            self._url = None
