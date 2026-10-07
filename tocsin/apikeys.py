from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tocsin.models import ApiKey

KEY_PREFIX = "tsn_"
SHOWN_PREFIX_LENGTH = 10

# Recording every use would turn each API read into a write; once a minute is
# precise enough to tell a key that is in use from one that is not.
LAST_USED_RESOLUTION = timedelta(minutes=1)


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


# Keys are 256 random bits, so a plain SHA-256 is enough: there is nothing to
# brute-force that a slow password hash would protect.
def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# Creates a key and returns it with its plaintext, which is shown once and
# never stored.
async def create_key(session: AsyncSession, name: str) -> tuple[ApiKey, str]:
    plaintext = generate_key()
    key = ApiKey(name=name, prefix=plaintext[:SHOWN_PREFIX_LENGTH], key_hash=hash_key(plaintext))
    session.add(key)
    await session.flush()
    return key, plaintext


async def authenticate(session: AsyncSession, plaintext: str) -> ApiKey | None:
    if not plaintext.startswith(KEY_PREFIX):
        return None
    key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(plaintext)))
    if key is None or key.revoked_at is not None:
        return None

    now = datetime.now(UTC)
    if key.last_used_at is None or now - key.last_used_at > LAST_USED_RESOLUTION:
        key.last_used_at = now
        await session.commit()
    return key
