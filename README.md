# Tocsin

Your cron jobs tell tocsin "I ran". Tocsin tells you when one of them didn't, and for evaluation
jobs, when the numbers got worse.

> **Status: early work in progress.** The ping API and check management work and are tested.
> Nothing sends alerts yet; that comes next. The [roadmap](#roadmap) below is kept up to date.
> Don't rely on this to watch anything important yet.

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

## What works today

- Checks with a cron schedule (`0 3 * * *`, `@daily`) or an interval (`@every 90m`), in any
  timezone. Daylight saving changes are handled the way cron handles them, and tested.
- Ping endpoints that need no key, since the ping URL itself is the secret:

  ```
  /ping/<uuid>            success
  /ping/<uuid>/start      job started
  /ping/<uuid>/fail       job failed
  /ping/<uuid>/<code>     exit code (0 is success)
  ```

  They accept GET, POST and HEAD. A POST body is kept as the run's output tail (the last 10 KB).
- State changes (`new`, `up`, `late`, `down`, `paused`) recorded as events.
- A JSON API for checks (`/api/checks`), protected by API keys.

## Roadmap

| Phase | Contents | Status |
|---|---|---|
| 1. Core | Checks, ping endpoints, API keys | Done |
| | Overdue sweep with row locking, exactly one alert per failure | Next |
| | Telegram alerts through a queue, with retries | Planned |
| | Rate limiting, metrics, CI, Docker Compose | Planned |
| 2. Usable | Start/finish runs and durations, CLI wrapper, flapping rules, self-monitoring | Planned |
| 3. Dashboard | Overview, check page, alert log, live updates | Planned |
| 4. Eval checks | Result schema, statistics, alert rules, JUnit and Stochast adapters | Planned |
| 5. Summaries | Redaction, LLM summaries (Claude, OpenAI and others), evaluation of the summariser | Planned |

## Trying it now

There is no Docker setup yet. You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and a
Postgres database.

```
uv sync --all-extras
export TOCSIN_DATABASE_URL=postgresql+asyncpg://user:password@localhost/tocsin
uv run tocsin migrate
export KEY=$(uv run tocsin keys create laptop)   # the key is shown only once
uv run tocsin api
```

Then create a check and ping it:

```
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"name": "backup", "schedule": "0 3 * * *", "timezone": "Europe/Lisbon"}' \
  http://127.0.0.1:8000/api/checks
curl http://127.0.0.1:8000/ping/<ping uuid from the response>
```

## Development

```
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run mypy tocsin
```

The tests run against a real Postgres, because the parts worth testing (row locks, `SKIP LOCKED`,
unique constraints) are Postgres behaviour. Set `TOCSIN_TEST_DATABASE_URL` to use your own;
otherwise the test suite starts a private one from the `pgserver` wheel, so no Docker or system
install is needed.

## License

MIT
