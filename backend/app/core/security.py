"""Security utilities: AES-256-GCM Credential Vault and Webhook HMAC-SHA256 verification."""

import base64
import hashlib
import hmac
import os
import time
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


class VaultDecryptionError(Exception):
    """Raised when encrypted credential data is tampered with or decrypted with the wrong key."""


class WebhookAuthenticationError(Exception):
    """Raised when an incoming webhook fails HMAC verification or replay timestamp checks."""


class CredentialVault:
    """AES-256-GCM envelope encryption vault for exchange API keys, secrets, and passphrases.

    Format of encrypted payload (base64-urlsafe encoded):
    [16-byte random PBKDF2 salt] + [12-byte random GCM nonce] + [ciphertext + 16-byte GCM auth tag]
    """

    SALT_LENGTH = 16
    NONCE_LENGTH = 12
    KEY_LENGTH = 32  # 256 bits
    KDF_ITERATIONS = 200_000

    def __init__(self, master_secret: str) -> None:
        if not master_secret or len(master_secret.strip()) < 16:
            raise ValueError("Master vault secret must be at least 16 characters long.")
        self._master_secret_bytes = master_secret.encode("utf-8")

    def _derive_key(self, salt: bytes) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=self.KEY_LENGTH,
            salt=salt,
            iterations=self.KDF_ITERATIONS,
        )
        return kdf.derive(self._master_secret_bytes)

    def encrypt(self, plaintext: str, associated_data: str | None = None) -> str:
        """Encrypt a plaintext string using AES-256-GCM with a fresh random salt and nonce."""
        if not plaintext:
            raise ValueError("Cannot encrypt empty plaintext.")

        salt = os.urandom(self.SALT_LENGTH)
        nonce = os.urandom(self.NONCE_LENGTH)
        key = self._derive_key(salt)
        aesgcm = AESGCM(key)
        aad = associated_data.encode("utf-8") if associated_data else None
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
        combined = salt + nonce + ciphertext
        return base64.urlsafe_b64encode(combined).decode("ascii")

    def decrypt(self, encrypted_token: str, associated_data: str | None = None) -> str:
        """Decrypt and authenticate an AES-256-GCM token."""
        try:
            raw = base64.urlsafe_b64decode(encrypted_token.encode("ascii"))
        except Exception as exc:
            raise VaultDecryptionError("Malformed base64 encrypted credential token.") from exc

        min_len = self.SALT_LENGTH + self.NONCE_LENGTH + 16  # 16-byte GCM tag minimum
        if len(raw) <= min_len:
            raise VaultDecryptionError("Encrypted credential payload is truncated or invalid.")

        salt = raw[: self.SALT_LENGTH]
        nonce = raw[self.SALT_LENGTH : self.SALT_LENGTH + self.NONCE_LENGTH]
        ciphertext = raw[self.SALT_LENGTH + self.NONCE_LENGTH :]

        key = self._derive_key(salt)
        aesgcm = AESGCM(key)
        aad = associated_data.encode("utf-8") if associated_data else None
        try:
            plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, aad)
            return plaintext_bytes.decode("utf-8")
        except InvalidTag as exc:
            raise VaultDecryptionError(
                "Credential authentication failed: wrong master secret, mismatched AAD, or tampered ciphertext."
            ) from exc


def mask_api_key(api_key: str) -> str:
    """Return a masked representation of an API key (e.g. 'abcd****wxyz') safe for UI display and logs."""
    cleaned = api_key.strip()
    if len(cleaned) <= 8:
        return "****"
    return f"{cleaned[:4]}****{cleaned[-4:]}"


def compute_webhook_signature(secret: str, timestamp: int, raw_body: bytes) -> str:
    """Compute HMAC-SHA256 hex signature over '<timestamp>.<raw_body>'."""
    signing_input = f"{timestamp}.".encode("utf-8") + raw_body
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    secret: str,
    timestamp: int,
    raw_body: bytes,
    provided_signature: str,
    max_skew_seconds: int = 300,
    now_ts: int | None = None,
) -> bool:
    """Verify webhook timestamp freshness and constant-time HMAC-SHA256 signature."""
    current_time = int(time.time()) if now_ts is None else now_ts
    skew = abs(current_time - int(timestamp))
    if skew > max_skew_seconds:
        raise WebhookAuthenticationError(
            f"Webhook timestamp skew ({skew}s) exceeds maximum allowed window ({max_skew_seconds}s)."
        )

    expected_sig = compute_webhook_signature(secret, timestamp, raw_body)
    if not hmac.compare_digest(expected_sig, provided_signature.strip().lower()):
        raise WebhookAuthenticationError("Webhook HMAC signature verification failed.")
    return True
