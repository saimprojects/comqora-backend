"""Encrypted provider credentials; the decrypted value never belongs in a serializer."""

import base64
import hmac

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


def cipher():
    # Domain-separated key derivation from Django's high-entropy server secret.
    secrets = [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    return MultiFernet(
        [
            Fernet(
                base64.urlsafe_b64encode(
                    hmac.digest(secret.encode(), b"comqora.assistant.credentials.v1", "sha256")
                )
            )
            for secret in secrets
        ]
    )


def encrypt_key(value):
    return cipher().encrypt(value.encode()).decode() if value else ""


def decrypt_key(value):
    if not value:
        return ""
    try:
        return cipher().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, UnicodeError):
        # Wrong server key/corrupt ciphertext must fail closed, never become an API key.
        return ""
