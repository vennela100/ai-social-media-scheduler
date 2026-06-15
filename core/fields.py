"""
Transparent at-rest encryption for sensitive text columns (OAuth tokens).

Usage:
    access_token = EncryptedTextField(blank=True, default="")

The value you assign in Python is plaintext; what lands in the database is a
Fernet token (URL-safe base64). Reads transparently decrypt. If the encryption
key is missing or a stored value can't be decrypted, we fail LOUD rather than
returning a silently-wrong token — a corrupted/forged token must never be used.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _fernet() -> Fernet:
    key = settings.TOKEN_ENCRYPTION_KEY
    if not key:
        raise ImproperlyConfigured(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"\n'
            "then add it to your .env / Render env / GitHub Secrets."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


class EncryptedTextField(models.TextField):
    """A TextField whose contents are encrypted in the database."""

    # Mark a sentinel so we can tell "already encrypted" from plaintext on save.
    _PREFIX = "enc::"

    def get_prep_value(self, value):
        """Python value -> DB value (encrypt)."""
        if value is None or value == "":
            return value
        token = _fernet().encrypt(str(value).encode()).decode()
        return self._PREFIX + token

    def from_db_value(self, value, expression, connection):
        """DB value -> Python value (decrypt)."""
        if value is None or value == "":
            return value
        if not value.startswith(self._PREFIX):
            # Legacy/plaintext row written before encryption was enabled.
            return value
        token = value[len(self._PREFIX):]
        try:
            return _fernet().decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError(
                "Failed to decrypt an EncryptedTextField — wrong/rotated "
                "TOKEN_ENCRYPTION_KEY or tampered data."
            ) from exc
