"""Security utilities: AES-256-GCM Credential Vault and Webhook HMAC-SHA256 verification."""

import base64
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets
import time
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)


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

    def __init__(self, master_secret: str, fallback_secrets: list[str] | None = None) -> None:
        if not master_secret or len(master_secret.strip()) < 16:
            raise ValueError("Master vault secret must be at least 16 characters long.")
        self._master_secret_bytes = master_secret.encode("utf-8")
        # Older secrets that may still decrypt rows saved before a secret change. Never used
        # to encrypt.
        self._fallback_secret_bytes = [s.encode("utf-8") for s in (fallback_secrets or []) if s]

    def _derive_key(self, salt: bytes, secret: bytes | None = None) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=self.KEY_LENGTH,
            salt=salt,
            iterations=self.KDF_ITERATIONS,
        )
        return kdf.derive(secret or self._master_secret_bytes)

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

        aad = associated_data.encode("utf-8") if associated_data else None
        for secret in [self._master_secret_bytes, *self._fallback_secret_bytes]:
            aesgcm = AESGCM(self._derive_key(salt, secret))
            try:
                return aesgcm.decrypt(nonce, ciphertext, aad).decode("utf-8")
            except InvalidTag:
                continue
        raise VaultDecryptionError(
            "Credential authentication failed: wrong master secret, mismatched AAD, or tampered ciphertext."
        )


# The built-in development secret. It is in the public source code, so anything encrypted
# with it is readable by anyone holding the database file.
DEFAULT_DEV_VAULT_SECRET = "dev-only-change-in-production-master-vault-secret-32b!"


def _default_vault_key_path(database_url: str) -> Path:
    """Next to the SQLite database (on the Docker data volume), else the working folder."""
    if database_url.startswith("sqlite") and ":///" in database_url:
        db_path = database_url.split(":///", 1)[1]
        if db_path and db_path != ":memory:":
            return Path(db_path).expanduser().resolve().parent / "vault.key"
    return Path("vault.key").resolve()


def load_or_create_vault_secret(path: Path) -> str:
    """Read the machine-local vault secret, creating a random one on first use."""
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if len(secret) >= 32:
            return secret
        raise ValueError(f"Vault key file {path} is too short; restore it from a backup.")
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    # Create exclusively so two processes starting together cannot write different secrets.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret)
    logger.warning(
        "Created a new API key vault secret at %s. Back it up: without it saved API keys cannot be read.", path
    )
    return secret


def build_credential_vault(
    master_vault_secret: str, database_url: str, vault_key_file: str | None = None
) -> CredentialVault:
    """The app's vault: an explicit MASTER_VAULT_SECRET, or else a random secret kept in a
    local file (vault.key, never committed). The public development secret is only accepted
    as a fallback, to read keys that were saved with it before.
    """
    if master_vault_secret and master_vault_secret != DEFAULT_DEV_VAULT_SECRET:
        return CredentialVault(master_vault_secret, fallback_secrets=[DEFAULT_DEV_VAULT_SECRET])
    path = Path(vault_key_file).expanduser().resolve() if vault_key_file else _default_vault_key_path(database_url)
    return CredentialVault(load_or_create_vault_secret(path), fallback_secrets=[DEFAULT_DEV_VAULT_SECRET])


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
