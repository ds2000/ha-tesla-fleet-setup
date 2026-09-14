"""Tests for EC P-256 key generation."""

import stat

import keygen
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key


class TestKeyGeneration:
    def test_generates_key_pair(self):
        private_pem, public_pem = keygen.ensure_keys()
        assert "BEGIN PRIVATE KEY" in private_pem
        assert "BEGIN PUBLIC KEY" in public_pem

    def test_private_key_is_ec_p256(self):
        private_pem, _ = keygen.ensure_keys()
        key = load_pem_private_key(private_pem.encode(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        assert isinstance(key.curve, ec.SECP256R1)

    def test_public_key_matches_private(self):
        private_pem, public_pem = keygen.ensure_keys()
        priv = load_pem_private_key(private_pem.encode(), password=None)
        pub = load_pem_public_key(public_pem.encode())
        # Public numbers should match
        assert priv.public_key().public_numbers() == pub.public_numbers()

    def test_keys_persisted_to_disk(self):
        keygen.ensure_keys()
        assert keygen.PRIVATE_KEY_PATH.exists()
        assert keygen.PUBLIC_KEY_PATH.exists()

    def test_private_key_permissions(self):
        keygen.ensure_keys()
        mode = keygen.PRIVATE_KEY_PATH.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_idempotent(self):
        pem1, pub1 = keygen.ensure_keys()
        pem2, pub2 = keygen.ensure_keys()
        assert pem1 == pem2
        assert pub1 == pub2

    def test_get_public_key(self):
        _, expected = keygen.ensure_keys()
        result = keygen.get_public_key()
        assert result == expected

    def test_regeneration_backs_up_and_replaces_pair(self):
        original = keygen.ensure_keys()
        replacement = keygen.ensure_keys(regenerate=True)
        assert replacement[0] != original[0]
        assert replacement[1] != original[1]
        assert keygen.ensure_keys() == replacement
        backup, = keygen.KEYS_DIR.glob("backup-*")
        assert (backup / "private.pem").read_text() == original[0]
        assert (backup / "public.pem").read_text() == original[1]
        assert stat.S_IMODE(backup.stat().st_mode) == 0o700
        assert stat.S_IMODE((backup / "private.pem").stat().st_mode) == 0o600
        priv = load_pem_private_key(replacement[0].encode(), password=None)
        pub = load_pem_public_key(replacement[1].encode())
        assert priv.public_key().public_numbers() == pub.public_numbers()

    def test_backup_failure_keeps_original_pair(self):
        from unittest.mock import patch

        import pytest

        original = keygen.ensure_keys()
        with patch.object(keygen, "_write_secure", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                keygen.ensure_keys(regenerate=True)
        assert keygen.ensure_keys() == original

    def test_keys_not_empty(self):
        private_pem, public_pem = keygen.ensure_keys()
        assert len(private_pem) > 100
        assert len(public_pem) > 100
