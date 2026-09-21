"""WhatsApp Flow encryption (RSA-OAEP SHA-256 + AES-128-GCM)."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

logger = logging.getLogger(__name__)


class FlowEncryptionError(Exception):
    pass


def _load_private_key():
    pem = (getattr(settings, "WHATSAPP_FLOW_PRIVATE_KEY_PEM", "") or "").strip()
    if not pem:
        path = (getattr(settings, "WHATSAPP_FLOW_PRIVATE_KEY_PATH", "") or "").strip()
        if path:
            with open(path, "rb") as fh:
                pem_bytes = fh.read()
        else:
            raise FlowEncryptionError("WHATSAPP_FLOW_PRIVATE_KEY_PEM / PATH not configured")
    else:
        # Allow \n escaped in .env
        pem_bytes = pem.replace("\\n", "\n").encode("utf-8")

    password = getattr(settings, "WHATSAPP_FLOW_PRIVATE_KEY_PASSWORD", None)
    pwd = password.encode("utf-8") if password else None
    try:
        return serialization.load_pem_private_key(pem_bytes, password=pwd)
    except Exception as exc:  # noqa: BLE001
        raise FlowEncryptionError(f"Invalid WhatsApp Flow private key: {exc}") from exc


def decrypt_request(body: dict[str, Any]) -> tuple[dict[str, Any], bytes, bytes]:
    """
    Decrypt a Meta Flow request body.

    Returns (payload_dict, aes_key, iv).
    Response encryption reuses aes_key and flipped iv.
    """
    try:
        encrypted_aes_key = base64.b64decode(body["encrypted_aes_key"])
        encrypted_flow_data = base64.b64decode(body["encrypted_flow_data"])
        iv = base64.b64decode(body["initial_vector"])
    except (KeyError, ValueError, TypeError) as exc:
        raise FlowEncryptionError(f"Malformed encrypted Flow request: {exc}") from exc

    private_key = _load_private_key()
    try:
        aes_key = private_key.decrypt(
            encrypted_aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise FlowEncryptionError(f"AES key decrypt failed: {exc}") from exc

    # ciphertext || 16-byte tag
    if len(encrypted_flow_data) < 17:
        raise FlowEncryptionError("encrypted_flow_data too short")
    ciphertext, tag = encrypted_flow_data[:-16], encrypted_flow_data[-16:]
    try:
        plaintext = AESGCM(aes_key).decrypt(iv, ciphertext + tag, None)
    except Exception as exc:  # noqa: BLE001
        raise FlowEncryptionError(f"Flow payload decrypt failed: {exc}") from exc

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FlowEncryptionError(f"Flow payload is not JSON: {exc}") from exc

    return payload, aes_key, iv


def encrypt_response(response: dict[str, Any], aes_key: bytes, iv: bytes) -> str:
    """Encrypt Flow response; Meta expects flipped IV and base64 ciphertext+tag."""
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    plaintext = json.dumps(response, separators=(",", ":")).encode("utf-8")
    encrypted = AESGCM(aes_key).encrypt(flipped_iv, plaintext, None)
    return base64.b64encode(encrypted).decode("ascii")
