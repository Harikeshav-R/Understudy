"""Identifier generators using standard ULID formatting."""

import os
import time

CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_ulid(timestamp_ms: int | None = None, random_bytes: bytes | None = None) -> str:
    """Generate a standard 26-character Crockford Base32 ULID string.

    ULID is composed of:
    - 48-bit timestamp in milliseconds (10 characters)
    - 80-bit cryptographic entropy (16 characters)
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    # 48 bits = 6 bytes for timestamp
    ts_bytes = timestamp_ms.to_bytes(6, byteorder="big")

    if random_bytes is None:
        random_bytes = os.urandom(10)
    elif len(random_bytes) != 10:
        msg = f"Random bytes must be exactly 10 bytes, got {len(random_bytes)}"
        raise ValueError(msg)

    combined = ts_bytes + random_bytes  # 16 bytes = 128 bits
    value = int.from_bytes(combined, byteorder="big")

    chars = []
    for _ in range(26):
        chars.append(CROCKFORD_BASE32[value & 0x1F])
        value >>= 5

    return "".join(reversed(chars)).lower()


def new_incident_id() -> str:
    """Generate a new unique incident identifier."""
    return f"inc_{generate_ulid()}"


def new_plan_id() -> str:
    """Generate a new unique remediation plan identifier."""
    return f"plan_{generate_ulid()}"


def new_twin_id() -> str:
    """Generate a new unique twin environment identifier."""
    return f"twin_{generate_ulid()}"


def new_run_id() -> str:
    """Generate a new unique run identifier."""
    return f"run_{generate_ulid()}"
