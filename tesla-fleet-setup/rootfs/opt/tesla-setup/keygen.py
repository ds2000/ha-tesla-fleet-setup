"""EC P-256 key pair generation for Tesla Fleet API."""

import os
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

KEYS_DIR = Path("/data/keys")
PRIVATE_KEY_PATH = KEYS_DIR / "private.pem"
PUBLIC_KEY_PATH = KEYS_DIR / "public.pem"


def _write_secure(path: Path, data: str, mode: int = 0o600):
    """Write file via temp with permissions set before content is written."""
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def ensure_keys(*, regenerate: bool = False) -> tuple[str, str]:
    """Return the EC P-256 pair; back up existing keys before explicit regeneration."""
    KEYS_DIR.mkdir(parents=True, exist_ok=True)

    if not regenerate and PRIVATE_KEY_PATH.exists() and PUBLIC_KEY_PATH.exists():
        return PRIVATE_KEY_PATH.read_text(), PUBLIC_KEY_PATH.read_text()

    if regenerate:
        backup_dir = Path(tempfile.mkdtemp(prefix="backup-", dir=KEYS_DIR))
        for path in (PRIVATE_KEY_PATH, PUBLIC_KEY_PATH):
            if path.exists():
                _write_secure(backup_dir / path.name, path.read_text())

    private_key = ec.generate_private_key(ec.SECP256R1())

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    _write_secure(PRIVATE_KEY_PATH, private_pem, 0o600)

    tmp_pub = PUBLIC_KEY_PATH.with_suffix(".tmp")
    tmp_pub.write_text(public_pem)
    os.replace(tmp_pub, PUBLIC_KEY_PATH)

    return private_pem, public_pem


def get_public_key() -> str:
    """Return the public key PEM, generating if needed."""
    _, public_pem = ensure_keys()
    return public_pem
