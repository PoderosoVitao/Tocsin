from __future__ import annotations

import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


# Counts hits for `key` in fixed windows of `window` seconds and reports
# whether this hit is within `limit`. A fixed window can let up to twice the
# limit through across a window boundary; for keeping a misbehaving job from
# flooding the database that is precise enough, and it costs one round trip.
#
# Fails open: if Redis is down, the hit is allowed. Losing rate limiting for a
# while is better than refusing pings and raising false "job is down" alerts.
async def allow(redis: Redis, key: str, limit: int, window: int = 60) -> bool:
    bucket = int(time.time() // window)
    name = f"tocsin:ratelimit:{key}:{bucket}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(name)
            pipe.expire(name, window * 2)
            count, _ = await pipe.execute()
    except (RedisError, OSError) as exc:
        log.warning("rate limiter unavailable, allowing request: %s", exc)
        return True
    return int(count) <= limit
