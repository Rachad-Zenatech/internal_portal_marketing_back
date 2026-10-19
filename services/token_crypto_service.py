"""AES-256-GCM token encryption service for OAuth access and refresh tokens."""
import base64
import hashlib
import os
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

_NONCE_SIZE = 12  # 96 bits recommended for GCM


def _derive_key() -> bytes:
    """Derive a 256-bit AES key from environment secrets using HKDF."""
    raw_val = (
        os.getenv("OAUTH_ENCRYPTION_KEY")
        or os.getenv("JWT_SECRET")
        or os.getenv("SESSION_SECRET")
    )
    if not raw_val or len(raw_val.strip()) < 16:
        if os.getenv("APP_ENV", "development").lower() == "production":
            raise RuntimeError(
                "OAUTH_ENCRYPTION_KEY (or JWT_SECRET/SESSION_SECRET of at least 16 chars) "
                "must be configured in production for OAuth token encryption."
            )
        raw_val = "default-dev-oauth-encryption-key-32b!!"

    raw_secret = raw_val.encode("utf-8")

    salt = b"marketing-portal-oauth-salt-v1"
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"oauth-token-encryption",
    )
    return hkdf.derive(raw_secret)


def encrypt_token(plain_token: str | None) -> str | None:
    """Encrypts a plaintext token string with AES-256-GCM.
    Returns URL-safe base64 encoded string containing nonce + ciphertext + tag.
    """
    if not plain_token:
        return None
    key = _derive_key()
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(_NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, plain_token.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("utf-8")


def decrypt_token(encrypted_token: str | None) -> str | None:
    """Decrypts a base64 encoded AES-256-GCM token string.
    Returns original plaintext token string.
    """
    if not encrypted_token:
        return None
    try:
        raw_data = base64.urlsafe_b64decode(encrypted_token.encode("utf-8"))
        if len(raw_data) < _NONCE_SIZE:
            return None
        nonce = raw_data[:_NONCE_SIZE]
        ciphertext = raw_data[_NONCE_SIZE:]
        key = _derive_key()
        aesgcm = AESGCM(key)
        decrypted_bytes = aesgcm.decrypt(nonce, ciphertext, None)
        return decrypted_bytes.decode("utf-8")
    except Exception:
        return None
