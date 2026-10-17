"""Keys and ciphers for the audit log's personal fields (SEC-07; technical design §10).

Each person gets a random 256-bit data key, stored only wrapped by the tenant's ``audit`` key.
Personal fields are encrypted with AES-256-GCM under the person's key, bound to the tenant,
record, subject and key version, so a ciphertext can't be moved to another record. Searchable
fields also get a blind index: an HMAC under a per-tenant key, so search works without
decryption.

Wrapping goes through ``KeyWrapper``. In AWS that is KMS Encrypt/Decrypt under the tenant's
``audit`` key with the tenant in the encryption context; ``LocalKeyWrapper`` stands in for local
development and tests only.
"""

import hashlib
import hmac
import json
import os
from typing import Any, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12


class KeyWrapper(Protocol):
    """Wraps and unwraps data keys under a tenant's audit key."""

    def wrap(self, tenant_id: UUID, purpose: str, key: bytes) -> bytes: ...

    def unwrap(self, tenant_id: UUID, purpose: str, wrapped: bytes) -> bytes: ...


class LocalKeyWrapper:
    """AES-GCM key wrapping under one local master key. For development and tests only."""

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != KEY_BYTES:
            raise ValueError("the master key must be 32 bytes")
        self._aead = AESGCM(master_key)

    def wrap(self, tenant_id: UUID, purpose: str, key: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, key, _context(tenant_id, purpose))

    def unwrap(self, tenant_id: UUID, purpose: str, wrapped: bytes) -> bytes:
        nonce, sealed = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        return self._aead.decrypt(nonce, sealed, _context(tenant_id, purpose))


def _context(tenant_id: UUID, purpose: str) -> bytes:
    return f"collicity-audit|{tenant_id}|{purpose}".encode()


def new_key() -> bytes:
    return os.urandom(KEY_BYTES)


def _binding(tenant_id: UUID, record_id: UUID, subject_id: UUID, version: int) -> bytes:
    return f"collicity-audit-record|{tenant_id}|{record_id}|{subject_id}|{version}".encode()


def encrypt_fields(
    key: bytes,
    *,
    tenant_id: UUID,
    record_id: UUID,
    subject_id: UUID,
    version: int,
    fields: dict[str, Any],
) -> bytes:
    """AES-256-GCM over the JSON of ``fields``: 12-byte nonce, then ciphertext and tag."""
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    binding = _binding(tenant_id, record_id, subject_id, version)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, binding)


def decrypt_fields(
    key: bytes,
    ciphertext: bytes,
    *,
    tenant_id: UUID,
    record_id: UUID,
    subject_id: UUID,
    version: int,
) -> dict[str, Any]:
    nonce, sealed = ciphertext[:NONCE_BYTES], ciphertext[NONCE_BYTES:]
    binding = _binding(tenant_id, record_id, subject_id, version)
    return json.loads(AESGCM(key).decrypt(nonce, sealed, binding))


def normalize(value: str) -> str:
    """Searchable values compare case- and whitespace-insensitively (emails, names)."""
    return " ".join(value.split()).casefold()


def blind_token(key: bytes, field: str, value: str) -> bytes:
    """HMAC-SHA256 of the field name and normalized value under the tenant's blind-index key."""
    message = field.encode() + b"\x00" + normalize(value).encode()
    return hmac.new(key, message, hashlib.sha256).digest()
