"""Identifiers.

Collicity generates time-ordered UUIDv7 identifiers (RFC 9562) in the application, so primary-key
indexes stay append-mostly. Python 3.13's ``uuid`` module has no ``uuid7`` (it arrives in 3.14).
"""

import os
import time
from uuid import UUID

_TIMESTAMP_MASK = (1 << 48) - 1
_RAND_A_MASK = (1 << 12) - 1
_RAND_B_MASK = (1 << 62) - 1


def new_id() -> UUID:
    """Return a new UUIDv7: 48-bit Unix milliseconds, version, 12 + 62 random bits, variant."""
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10))
    value = (
        (unix_ms & _TIMESTAMP_MASK) << 80
        | 0x7 << 76
        | ((rand >> 62) & _RAND_A_MASK) << 64
        | 0b10 << 62
        | (rand & _RAND_B_MASK)
    )
    return UUID(int=value)
