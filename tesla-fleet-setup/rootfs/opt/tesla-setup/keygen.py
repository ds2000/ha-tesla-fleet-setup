"""EC P-256 key pair generation for Tesla Fleet API."""

import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

KEYS_DIR = Path("/data/keys")
PRIVATE_KEY_PATH = KEYS_DIR / "private.pem"
PUBLIC_KEY_PATH = KEYS_DIR / "public.pem"


def ensure_keys() -> tuple[str, str]:
    """Generate EC P-256 key pair if not already present. Returns (private_pem, public_pem)."""
    KEYS_DIR.mkdir(parents=True, exist_ok=True)

    if PRIVATE_KEY_PATH.exists() and PUBLIC_KEY_PATH.exists():
        return PRIVATE_KEY_PATH.read_text(), PUBLIC_KEY_PATH.read_text()

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

    # Write atomically-ish: write to temp then rename
    tmp_priv = PRIVATE_KEY_PATH.with_suffix(".tmp")
    tmp_pub = PUBLIC_KEY_PATH.with_suffix(".tmp")

    tmp_priv.write_text(private_pem)
    os.replace(tmp_priv, PRIVATE_KEY_PATH)
    os.chmod(PRIVATE_KEY_PATH, 0o600)

    tmp_pub.write_text(public_pem)
    os.replace(tmp_pub, PUBLIC_KEY_PATH)

    return private_pem, public_pem


def get_public_key() -> str:
    """Return the public key PEM, generating if needed."""
    _, public_pem = ensure_keys()
    return public_pem
