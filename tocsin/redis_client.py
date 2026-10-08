from __future__ import annotations

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff


# A client for request paths that must not wait on Redis. redis-py retries
# failed commands with backoff by default, which would hold every ping for
# seconds while Redis is down; here a command fails within half a second and
# the caller decides what to do without it.
def fast_failing(url: str) -> Redis:
    return Redis.from_url(
        url,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        retry=Retry(NoBackoff(), retries=0),
    )
