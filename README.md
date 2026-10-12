# Tocsin

Your cron jobs tell tocsin "I ran". Tocsin tells you when one of them didn't, and for evaluation
jobs, when the numbers got worse.

> **Status: work in progress.** The core works and is tested: tocsin notices when a job misses
> its schedule or reports a failure, and sends exactly one Telegram alert for it. The CLI wrapper,
> dashboard, evaluation checks and failure summaries are still to come (see the
> [roadmap](#roadmap)). Tocsin doesn't monitor itself yet, so don't make it the only thing
> watching something important.

## Why another job monitor

Dead man's switch monitors already exist, and [healthchecks.io](https://healthchecks.io) is the
mature open-source one. If you only need to know whether a job ran, use that.

Tocsin is built for scheduled AI work, where "it ran" isn't enough:

- **Evaluation checks.** A nightly evaluation run reports its pass rate, sample size, latency and
  cost. Tocsin alerts on a statistically meaningful regression, and says when it needs more data
  instead of guessing.
- **Failure summaries.** When a job fails, the alert carries a one-line, machine-generated
  explanation written from the captured output, after redacting secrets.
- **A CLI wrapper** that makes any command report its start, exit code and output.

## Quick start

```
git clone https://github.com/PoderosoVitao/Tocsin && cd Tocsin
docker compose up -d
docker compose exec api tocsin keys create admin    # prints an API key, once
```

That starts the API on port 8000 with Postgres, Redis, a worker and a scheduler. Before using it
for real, put `TOCSIN_PUBLIC_URL` (the address your jobs reach tocsin at) and a
`POSTGRES_PASSWORD` in a `.env` file next to `docker-compose.yml`.

Add a Telegram chat to alert. Create a bot with [@BotFather](https://t.me/BotFather) to get a
token, send the bot a message, and read your chat id from
`https://api.telegram.org/bot<token>/getUpdates`. Then:

```
export KEY=<the key from above>
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"kind": "telegram", "name": "phone", "config": {"bot_token": "<token>", "chat_id": "<chat id>"}}' \
  http://localhost:8000/api/channels
curl -s -X POST -H "Authorization: Bearer $KEY" http://localhost:8000/api/channels/<channel id>/test
```

Create a check for a job, and have the job ping its URL when it finishes:

```
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"name": "backup", "schedule": "0 3 * * *", "timezone": "Europe/Lisbon", "grace_seconds": 600}' \
  http://localhost:8000/api/checks
# in the job, report the exit code (0 is success):
./backup.sh; curl -fsS -m 10 --retry 3 <ping_url>/$?
```

## What works today

- **Checks** with a cron schedule (`0 3 * * *`, `@daily`) or an interval (`@every 90m`), in any
  timezone, with a grace period. Daylight saving changes are handled the way cron handles them,
  and tested: a job in the skipped hour is due right after the jump, and a fixed-time job in the
  repeated hour is due once.
- **Ping endpoints** that need no key, since the ping URL itself is the secret. They accept GET,
  POST and HEAD; a POST body is kept as the run's output tail (the last 10 KB).

  ```
  /ping/<uuid>            success
  /ping/<uuid>/start      job started
  /ping/<uuid>/fail       job failed
  /ping/<uuid>/<code>     exit code (0 is success)
  ```

- **Missed runs.** A sweep every five seconds moves checks whose deadline (due time plus grace)
  has passed to `down`.
- **Telegram alerts** when a check goes down and when it recovers, with the last lines of the
  job's output when it sent some. Failed sends are retried with exponential backoff and jitter for about 40 minutes,
  then marked failed.
- **States** `new`, `up`, `late` (past due, inside the grace period), `down` and `paused`, with
  every change recorded as an event.
- **A JSON API** for checks and channels, protected by API keys. Channel tokens are never returned.
- **Rate limiting** per check in Redis (60 pings a minute by default). If Redis is down, pings are
  still accepted.
- **Operations:** `/healthz`, and Prometheus metrics on `/metrics`, including
  `tocsin_checks_overdue_unswept`, which goes above zero when the sweep has stopped running.

## How a failure becomes exactly one alert

```
sweep, every 5 s (any number may run at once)
  SELECT ... FROM checks WHERE state = 'up' AND next_expected_at <= now()
    FOR UPDATE SKIP LOCKED
  for each row:  state -> down                      ┐
                 INSERT event                       │ one transaction
                 INSERT delivery per channel        ┘ unique (event_id, channel_id)
  COMMIT, then queue deliver(event, channel)

worker: deliver(event, channel)
  SELECT the delivery FOR UPDATE SKIP LOCKED
  already sent or failed?  ->  stop
  send  ->  sent | retrying at now + backoff | failed

relay, every 5 s
  queue deliveries whose retry is due, or whose first job never ran
```

- **One event.** A second sweep running at the same moment skips the rows the first one has
  locked instead of waiting for them; by the time the locks are released, those checks are down
  and no longer match. A ping that arrives during a sweep waits for it, then sees the check down
  and records a recovery.
- **One alert.** The state change, the event and its deliveries are written in one transaction,
  so they exist together or not at all. The delivery row, unique per (event, channel), is the
  idempotency key: a duplicated or retried job finds it already sent and does nothing.
- **Nothing lost.** Alerts are queued only after the commit. If queueing fails (Redis down, a
  crash in between), the delivery is still in Postgres and the relay queues it. Retries live in
  the database too, so they survive a restart.

The test suite runs four sweeps at once over forty overdue checks and then runs every delivery
job twice; it expects forty events and forty messages. With the row lock removed, the same test
gets 110 events.

One duplicate remains possible, by design: if a worker crashes after Telegram accepted a message
but before the commit, the message is sent again. A duplicate alert is better than a lost one.

The Redis lock around each sweep is only an optimisation, so extra sweeps return early instead
of competing for rows. Correctness never depends on it, because a Redis lock can expire in the
middle of a slow sweep.

## Why TaskIQ

The spec left the queue open: Celery, TaskIQ or Arq. Tocsin uses
[TaskIQ](https://taskiq-python.github.io/) because everything else is async (FastAPI, async
SQLAlchemy) and TaskIQ tasks are plain async functions; Celery would need a synchronous bridge to
the same database code. Within the async options, TaskIQ acknowledges a job only after it has run,
and its Redis streams broker hands a job that stays unacknowledged for ten minutes (because its
worker died) to another worker. Its scheduler also accepts intervals of seconds, which the
five-second sweep needs. The queue is never trusted for correctness, though: every
alert is a row in Postgres first.

## Limitations

- **Nobody watches the watcher yet.** If tocsin itself is down, nothing alerts you. Pinging an
  external monitor on a schedule is planned for the next phase.
- **Telegram is the only channel** so far. Email and outgoing webhooks are planned.
- **Start pings are recorded but not yet paired** with finish pings, so there are no durations
  and no "started but never finished" alerts yet.
- **Lenient in the repeated hour.** On the night the clocks go back, a frequent job may be
  detected as missing up to an hour late. This is the price of never raising a false alarm on
  that night.
- **`/metrics` is unauthenticated**, like most Prometheus endpoints. It exposes counts, never
  names or ping URLs; put it behind your reverse proxy if that matters.

## Roadmap

| Phase | Contents | Status |
|---|---|---|
| 1. Core | Checks, pings, overdue sweep, exactly-once Telegram alerts, API keys, rate limiting, metrics, CI, Docker Compose | Done |
| 2. Usable | Start/finish runs and durations, CLI wrapper, flapping rules, self-monitoring | Next |
| 3. Dashboard | Overview, check page, alert log, live updates | Planned |
| 4. Eval checks | Result schema, statistics, alert rules, JUnit and Stochast adapters | Planned |
| 5. Summaries | Redaction, LLM summaries (Claude, OpenAI and others), evaluation of the summariser | Planned |

## Development

```
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run mypy tocsin
scripts/smoke-test.sh    # builds and checks the whole Compose stack; needs Docker
```

To run the services without Docker, set `TOCSIN_DATABASE_URL` and `TOCSIN_REDIS_URL`, then
`uv run tocsin migrate` and start `tocsin api`, `tocsin worker` and `tocsin scheduler`.

The tests run against a real Postgres, because the parts worth testing (row locks, `SKIP LOCKED`,
unique constraints) are Postgres behaviour, and a real Redis. Set `TOCSIN_TEST_DATABASE_URL` and
`TOCSIN_TEST_REDIS_URL` to use your own. On Python 3.12 you can leave them unset: the suite starts
private servers from the `pgserver` and `redislite` wheels, so no Docker or system install is
needed.

## License

MIT
