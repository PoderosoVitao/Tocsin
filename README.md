# Tocsin

Your cron jobs tell tocsin "I ran". Tocsin tells you when one of them didn't, and for evaluation
jobs, when the numbers got worse.

> **Status: work in progress.** The core works and is tested: tocsin notices when a job misses
> its schedule, fails or runs too long, and sends exactly one Telegram alert for it, and a first
> dashboard page shows every check live. The check and alert-log pages, evaluation checks and
> failure summaries are still to come (see the
> [roadmap](#roadmap)).

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

Create a check for a job, and run the job through `tocsin run`:

```
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"name": "backup", "schedule": "0 3 * * *", "timezone": "Europe/Lisbon", "grace_seconds": 600}' \
  http://localhost:8000/api/checks
# on the machine that runs the job (the base install has no dependencies):
pipx install git+https://github.com/PoderosoVitao/Tocsin
tocsin run --check <ping uuid> --server http://localhost:8000 -- ./backup.sh
```

`tocsin run` sends a start ping, runs the command with its output passing through as usual, then
reports the exit code with the last 100 lines of output. If tocsin can't be reached, the command
still runs, a warning goes to stderr, and the command's own exit code is returned. A plain
`curl <ping_url>/$?` after the job works too.

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
- **Runs and durations.** A `start` ping followed by a finish ping makes a run with a duration.
  Set `max_duration_seconds` on a check to be alerted when a run takes longer, whether it is still
  going or has just finished. A job that starts and never finishes is reported when its next run
  was due. Run history is at `/api/checks/<id>/runs`.
- **Telegram alerts** when a check goes down and when it recovers, with the last lines of the
  job's output when it sent some. Failed sends are retried with exponential backoff and jitter for about 40 minutes,
  then marked failed.
- **Flapping control.** After an alert, further changes within the check's cooldown (five minutes
  by default) are recorded but not sent. When the cooldown ends, one alert says where the check
  settled, if that differs from the last message, with how many times it changed. An alert can be
  delayed by the cooldown but never dropped. Optional reminders (`reminder_seconds`) repeat the
  alert while a check stays down.
- **States** `new`, `up`, `late` (past due, inside the grace period), `down` and `paused`, with
  every change recorded as an event.
- **A JSON API** for checks and channels, protected by API keys. Channel tokens are never returned.
- **Dashboard** at `/` (for example http://localhost:8000): sign in with an API key, kept in the
  browser only. The overview lists every check with its state, last ping, next expected time and a
  strip of its recent runs, problems first, and updates live without reloading. It is plain HTML,
  CSS and JavaScript with no build step, and shares the Stochast dashboard's colours and dark mode.
  The check page, alert log and settings are still to come.
- **Live updates.** `GET /api/stream` is a Server-Sent Events stream of check changes
  (`check.updated`, `check.deleted`), fed by Redis pub/sub and published after each change is
  committed. The messages are hints to refresh, not data, and a lost one never fails a ping or a
  sweep. The stream needs an API key, so read it with `curl -N -H "Authorization: Bearer $KEY"`
  or `fetch`; the browser's `EventSource` can't send headers, which is why the dashboard uses
  `fetch`. `GET /api/overview` returns every check with its last 20 runs in one request, and
  `GET /api/checks/<id>/events` lists a check's state changes, newest first, each with the alert
  deliveries it caused: the channel, how many attempts, the last error and the next retry. Changes
  held back by the cooldown are marked `suppressed`.
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

The sweep's other two passes, the alert sent when a flapping check settles and the "still down"
reminder, claim their rows the same way and update them so they no longer match, so they are sent
once each too.

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

## Who watches the watcher

A monitor that has stopped looks exactly like a quiet night, so tocsin reports on itself to a
monitor outside it. Create a check on another service (healthchecks.io's free tier is enough, or
a second tocsin) with a one-minute period, and set its ping URL as `TOCSIN_SELF_PING_URL`. Give
that service a different way to reach you than tocsin's Telegram bot, such as email.

Every minute the scheduler queues a heartbeat and a worker runs it. If everything is fine, it
pings the URL; if not, it pings `<url>/fail` with a list of what is wrong, so you are alerted at
once with the reason instead of waiting for heartbeats to stop.

**Covered:** the scheduler, Redis and at least one worker (the heartbeat can't run without all
three); the database; the sweep (one has finished in the last two minutes, and no overdue check
is left up); alert delivery (any alert that failed to send in the last 15 minutes, and any left
with nothing retrying it); and the API's health endpoint, when `TOCSIN_SELF_CHECK_API_URL` is set
(Docker Compose sets it).

**Not covered:**

- The external monitor itself. If it is down or its alerts don't reach you, nothing tells you.
- Whether your jobs can reach tocsin. The API is checked from inside, not through the public
  address. A job that can't reach tocsin does show up, as a missed run, but only at its deadline.
- The server's clock. If it drifts, deadlines drift with it.
- Outages shorter than the external check's grace period, and anything between two heartbeats.

## Limitations

- **Telegram is the only channel** so far. Email and outgoing webhooks are planned.
- **One run at a time per check.** A second `start` before a finish marks the first run
  unfinished; overlapping runs of the same job aren't tracked separately.
- **Lenient in the repeated hour.** On the night the clocks go back, a frequent job may be
  detected as missing up to an hour late. This is the price of never raising a false alarm on
  that night.
- **`/metrics` is unauthenticated**, like most Prometheus endpoints. It exposes counts, never
  names or ping URLs; put it behind your reverse proxy if that matters.

## Roadmap

| Phase | Contents | Status |
|---|---|---|
| 1. Core | Checks, pings, overdue sweep, exactly-once Telegram alerts, API keys, rate limiting, metrics, CI, Docker Compose | Done |
| 2. Usable | Start/finish runs and durations, CLI wrapper, flapping rules, self-monitoring | Done |
| 3. Dashboard | Overview with live updates | Done |
| | Check page, alert log, settings | Next |
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
