"""Tesla HTTP Proxy management — runs tesla-http-proxy for signed vehicle commands."""

import asyncio
import datetime
import ipaddress
import logging
import os
import shutil
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

TLS_DIR = Path("/data/tls")
TLS_CERT_PATH = TLS_DIR / "cert.pem"
TLS_KEY_PATH = TLS_DIR / "key.pem"
COMMAND_KEY_PATH = Path("/data/keys/private.pem")
PROXY_BINARY = "/usr/local/bin/tesla-http-proxy"
PROXY_PORT = 4443


def proxy_available() -> bool:
    """Check if the tesla-http-proxy binary is installed."""
    return shutil.which("tesla-http-proxy") is not None or Path(PROXY_BINARY).exists()


TLS_CERT_VALIDITY_DAYS = 365
TLS_CERT_RENEWAL_DAYS = 30  # Regenerate when less than 30 days remain


def _cert_needs_renewal() -> bool:
    """Check if the existing TLS cert is expiring within the renewal window."""
    if not TLS_CERT_PATH.exists():
        return True
    try:
        cert = x509.load_pem_x509_certificate(TLS_CERT_PATH.read_bytes())
        remaining = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
        if remaining.days < TLS_CERT_RENEWAL_DAYS:
            logger.info("TLS cert expires in %d days — regenerating", remaining.days)
            return True
        return False
    except Exception as e:
        logger.warning("Could not read TLS cert for renewal check: %s", e)
        return True


def get_cert_info() -> dict | None:
    """Return TLS certificate expiry info, or None if no cert exists."""
    if not TLS_CERT_PATH.exists():
        return None
    try:
        cert = x509.load_pem_x509_certificate(TLS_CERT_PATH.read_bytes())
        now = datetime.datetime.now(datetime.timezone.utc)
        expiry = cert.not_valid_after_utc
        remaining = expiry - now
        return {
            "expires": expiry.isoformat(),
            "days_remaining": max(0, remaining.days),
            "valid": remaining.days > 0,
        }
    except Exception:
        return None


def ensure_tls_certs() -> tuple[str, str]:
    """Generate self-signed TLS certs for the proxy if not present or expiring soon.

    Cert validity: 1 year. Auto-regenerates when less than 30 days remain.
    Returns (cert_path, key_path).
    """
    TLS_DIR.mkdir(parents=True, exist_ok=True)

    if TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists() and not _cert_needs_renewal():
        return str(TLS_CERT_PATH), str(TLS_KEY_PATH)

    logger.info("Generating self-signed TLS certificate for tesla-http-proxy")

    key = ec.generate_private_key(ec.SECP256R1())

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "tesla-fleet-setup"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Home Assistant"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.DNSName("tesla-fleet-setup"),
                x509.IPAddress(ipaddress_from_str("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Write key with restricted permissions from creation
    tmp_key = str(TLS_KEY_PATH.with_suffix(".tmp"))
    fd = os.open(tmp_key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    finally:
        os.close(fd)
    os.replace(tmp_key, TLS_KEY_PATH)

    # Write cert
    tmp_cert = TLS_CERT_PATH.with_suffix(".tmp")
    tmp_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    os.replace(tmp_cert, TLS_CERT_PATH)

    logger.info("TLS certificate generated at %s", TLS_CERT_PATH)
    return str(TLS_CERT_PATH), str(TLS_KEY_PATH)


def ipaddress_from_str(addr: str):
    """Convert string IP to ipaddress object for SAN extension."""
    return ipaddress.ip_address(addr)


class ProxyManager:
    """Manages the tesla-http-proxy subprocess."""

    def __init__(self):
        self._process: asyncio.subprocess.Process | None = None
        self._log_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "available": proxy_available(),
            "port": PROXY_PORT,
            "url": f"https://localhost:{PROXY_PORT}" if self.running else None,
            "tls_cert": get_cert_info(),
        }

    async def start(self) -> dict:
        """Start the tesla-http-proxy. Returns status dict."""
        if self.running:
            return {"success": True, "message": "Proxy already running", **self.get_status()}

        if not proxy_available():
            return {"success": False, "error": "tesla-http-proxy binary not found"}

        if not COMMAND_KEY_PATH.exists():
            return {"success": False, "error": "Command authentication key not found. Complete setup first."}

        cert_path, key_path = ensure_tls_certs()

        cmd = [
            PROXY_BINARY,
            "-tls-key", key_path,
            "-cert", cert_path,
            "-key-file", str(COMMAND_KEY_PATH),
            "-port", str(PROXY_PORT),
            "-host", "0.0.0.0",
        ]

        logger.info("Starting tesla-http-proxy on port %d", PROXY_PORT)

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return {"success": False, "error": "tesla-http-proxy binary not found at expected path"}

        # Start background log reader
        self._log_task = asyncio.create_task(self._read_logs())

        # Give the proxy a moment to start (or fail)
        await asyncio.sleep(1.0)

        if self._process.returncode is not None:
            stderr = ""
            if self._process.stderr:
                try:
                    data = await asyncio.wait_for(self._process.stderr.read(2048), timeout=2)
                    stderr = data.decode(errors="replace")
                except asyncio.TimeoutError:
                    pass
            logger.error("tesla-http-proxy exited immediately: %s", stderr[:500])
            return {"success": False, "error": f"Proxy failed to start: {stderr[:500]}"}

        logger.info("tesla-http-proxy started (PID %d)", self._process.pid)
        return {"success": True, "message": "Proxy started", **self.get_status()}

    async def stop(self):
        """Stop the proxy."""
        if self._log_task:
            self._log_task.cancel()
            self._log_task = None

        if self._process:
            logger.info("Stopping tesla-http-proxy")
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            self._process = None

    async def _read_logs(self):
        """Read proxy stderr and forward to our logger."""
        if not self._process or not self._process.stderr:
            return
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if text:
                    logger.info("[proxy] %s", text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Proxy log reader stopped: %s", e)
