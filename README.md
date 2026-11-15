# Tocsin

Your cron jobs tell tocsin "I ran". Tocsin tells you when one of them didn't, and for evaluation
jobs, when the numbers got worse.

> **Status: work in progress.** The core works and is tested: tocsin notices when a job misses
> its schedule, fails or runs too long, and sends exactly one Telegram alert for it, and a
> dashboard shows and manages everything. Evaluation checks alert on their numbers and have a page of
> their own, and JUnit test results can be sent as evaluation reports. Stochast results can be
> sent the same way. Failure summaries are still to come (see the [roadmap](#roadmap)).

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
- **A JSON API** for checks and channels, protected by API keys. Keys can be created, listed and
  revoked through `/api/keys` as well as the CLI; the key is shown once when created, and the key
  you are using can't be revoked, so you can't lock yourself out. Channel tokens are never returned.
- **Dashboard** at `/` (for example http://localhost:8000): sign in with an API key, kept in the
  browser only. The overview lists every check with its state, last ping, next expected time and a
  strip of its recent runs, problems first, and updates live without reloading. It is plain HTML,
  CSS and JavaScript with no build step, and shares the Stochast dashboard's colours and dark mode.
  Each check has a page (`/#/checks/<id>`, the link in every alert) with its settings, a bar chart
  of run durations, the run list with expandable output tails, and its state changes with every
  alert delivery: which channel, how many attempts, the error, and when the next retry is. The
  **alert log** (`/#/alerts`) lists every alert across all checks, newest first, and can be filtered
  to failed, retrying, queued or sent ones, so an alert that never got through can't go unnoticed.
  The **settings** page (`/#/settings`) creates, edits, pauses and deletes checks (times in minutes
  and hours), adds Telegram chats and sends them a test message, and creates and revokes API keys.
  Secrets are never shown again: a bot token appears masked, and a new API key is displayed once.
- **Live updates.** `GET /api/stream` is a Server-Sent Events stream of check changes
  (`check.updated`, `check.deleted`), fed by Redis pub/sub and published after each change is
  committed. The messages are hints to refresh, not data, and a lost one never fails a ping or a
  sweep. The stream needs an API key, so read it with `curl -N -H "Authorization: Bearer $KEY"`
  or `fetch`; the browser's `EventSource` can't send headers, which is why the dashboard uses
  `fetch`. `GET /api/overview` returns every check with its last 20 runs in one request, and
  `GET /api/checks/<id>/events` lists a check's state changes, newest first, each with the alert
  deliveries it caused: the channel, how many attempts, the last error and the next retry. Changes
  held back by the cooldown are marked `suppressed`. `GET /api/alerts` is the same deliveries across
  every check, filterable with `?status=failed`. The stream also announces `alert.updated` when a
  worker sends, retries or gives up on a delivery, so retries show on the dashboard as they happen.
- **Evaluation reports.** A check created with `"kind": "eval"` expects a JSON report in the body of
  its success ping, in the vendor-neutral `tocsin.eval/v1` schema, and stores the counts for each
  scenario (not just a percentage, so tocsin can do its own statistics):

  ```
  {
    "schema": "tocsin.eval/v1",
    "suite": "refund-agent",
    "labels": {"model": "…", "prompt_version": "…", "git_sha": "…"},
    "scenarios": [
      {"name": "refund_tool_never_called", "runs": 50, "passes": 47,
       "latency_ms": {"p50": 820, "p95": 2400}, "cost_usd": {"total": 1.84}}
    ],
    "report_url": "https://ci.example.com/runs/812"
  }
  ```

  `latency_ms`, `cost_usd`, `labels` and `report_url` are optional, and fields tocsin doesn't know
  are kept in the stored report and otherwise ignored. A success ping with no report, or one that
  can't be read (bad JSON, `passes` above `runs`, a duplicate scenario name, over 1 MB), is
  recorded as a failed run: the sender gets a `422` saying why, and the alert says it too. A job
  whose reports have stopped being readable can't look healthy. Start and fail pings need no
  report. `GET /api/checks/<id>/eval` lists the reports, newest first, with each scenario's pass
  rate and 95% Wilson interval, the verdict and what the rules found.
- **Evaluation rules.** Each report is checked against the rules in the check's `eval_rules`
  (set when creating the check or with `PATCH`, which replaces them as a whole):

  | Rule | Fails the report when |
  |---|---|
  | `floor` | the upper end of a scenario's 95% interval is below it: under the bar even on the generous reading |
  | regression (on by default) | a scenario's pass rate fell against the baseline by at least `min_drop` (default 5 points) and the drop is statistically clear (one-sided Fisher exact test, `alpha` 0.05) |
  | `max_latency_ms` | a latency percentile in a scenario is over its limit, such as `{"p95": 3000}` |
  | `max_cost_usd` | the report's total cost is over the limit |

  The baseline is the previous `baseline_reports` reports (default 5) pooled, or one report pinned
  with `baseline_report_id`. Reports that failed are left out of the pool, so a regression that
  lasts doesn't become the new normal and quietly end the alert.

  A failing report takes the check down and sends the usual alert, with what was found. A drop
  that looks real but is too small a sample to call is **not enough data**: not an alert, but
  stored with the report and its findings, with about how many more runs would settle it. It isn't
  a recovery either: a check that is down stays down until a report is clearly fine. A missed run
  is the ordinary dead man's switch. A good report brings the check back up.
- **Eval page.** The page of an eval check (`/#/checks/<id>`) gains an Evaluation panel: the latest
  verdict with what the rules found, and for each scenario its pass rate with the 95% interval, the
  latency percentiles and cost, and a chart of the pass rate over the last 100 reports with the
  interval as a band, the baseline as a dashed line and a dotted line wherever a label changed (a new
  model, prompt version or commit). Failing reports are diamonds and "not enough data" ones are
  open circles, so the colours aren't the only signal. Below are "what changed" at each label
  change and the latest reports with their labels and a link to the full report. Scenarios with a
  finding come first, and the rest are behind "Show all".
- **JUnit adapter.** Most test tools can write JUnit XML (`pytest --junitxml`, Maven Surefire,
  Jest and Vitest reporters, `gotestsum`), so `tocsin report junit` turns those results into a
  `tocsin.eval/v1` report and sends it as the check's success ping:

  ```
  tocsin report junit --check <ping uuid> --server http://localhost:8000 \
    --label model=claude-sonnet-5-5 --label git_sha=$(git rev-parse --short HEAD) results.xml
  ```

  Each distinct test is a scenario, and a test that appears several times in the results (a
  repeated or parametrised run) gets a pass rate; a failure or an error is a failed run, a flaky
  test that a rerun passed counts as passed, and skipped tests are left out. Latency comes from
  the `time` attributes (p50 and p95). Several files make one report, `-` reads standard input,
  `--group-by class` makes each class one scenario for suites with more than 1000 tests, and
  `--dry-run` prints the report instead of sending it. XML that declares a DOCTYPE or entities is
  refused. If the results can't be read, tocsin is sent a failure with the reason, so the check
  goes down with it in the alert. The command exits 1 if tocsin refuses the report or can't be
  reached, so add `|| true` where an outage shouldn't fail your pipeline. It uses only the
  standard library, like `tocsin run`.
- **Stochast adapter.** [Stochast](https://github.com/PoderosoVitao/Stochast) saves each scenario's
  runs as JSON under `stochast-results/`. `tocsin report stochast` turns those files into a
  `tocsin.eval/v1` report, so a nightly Stochast run can be watched by tocsin without any change
  to Stochast:

  ```
  stochast run scenarios.py --adapter agent.py:build_adapter --fail-under 0
  tocsin report stochast stochast-results --check <ping uuid> --server http://localhost:8000 \
    --suite refund-agent --label model=gpt-4.1 --label prompt_version=v12
  ```

  Each scenario becomes a scenario with its number of runs and how many passed (Stochast's own
  definition: no error and every assertion held, so a run lost to a transport error counts as not
  passed). Latency is p50, p95 and p99 of the runs, worked out the way Stochast's report does,
  and the cost is the sum over the runs when Stochast was given token prices. The numbers match
  Stochast's own report for the same files, and tocsin's Wilson intervals use the same formula.
  Only counts and numbers are sent: outputs, tool calls and messages stay on the machine.
  Stochast doesn't record which model or prompt it ran, so name them with `--label`. A directory
  stands for the `.json` files in it, and a scenario that appears in more than one file is
  refused instead of added up. `--fail-under 0` keeps `stochast run` from stopping the job
  before the report is sent; whether the numbers are a regression is for tocsin to say.
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

## Redaction

Failure summaries will send a job's output to a third party's model, and that output can hold
anything the job saw. Before any of it leaves, `tocsin.redact` scrubs it. This is built and
tested, and the summaries that use it come next.

It removes, replacing each with a marker such as `[REDACTED:github-token]`:

- private keys (a PEM block, to its end or to the end of the output if it has none);
- credentials with a recognisable shape: AWS, GitHub, OpenAI, Anthropic, Slack, Google, Stripe and
  Telegram keys, JWTs, tocsin's own API keys, and the secret uuid in a tocsin ping URL;
- `user:password@` in a URL, `Authorization`, `Cookie` and similar headers, and `Bearer` or `Basic`
  values;
- the value of anything named like a secret: `password`, `passwd`, `secret`, `token`, `api_key`,
  `access_key`, `credentials`, `signature`, `session_id` and the like, as `NAME=value`,
  `"name": "value"` or `--name value`. The name has to be a word of its own, so `passed: 5` and
  `tokens: 812` are left alone;
- e-mail addresses, and terminal colour codes;
- any long run (20 characters or more) that looks random: letters and digits, enough entropy, and
  mixed case if it contains `/`, `-` or `_`. Paths, slugs and uuids are kept.

Then it keeps only the last 4000 characters, since the end of the output is where the error is.
Redaction runs on the whole text before it is cut, so a secret can't survive as a fragment.

It errs towards removing too much, and it can't catch everything. **A short password, or a
password that is an ordinary word, with nothing around it to say it is one, looks like text and
gets through.** So do secrets written in a form not listed above, and a password given as `-pSECRET`
with no space. Summaries stay off by default and are switched on per check, so you choose which
jobs' output may leave the machine. The tests plant 27 fake credentials in realistic places (error
messages, connection strings, command lines, JSON, a PEM block) and in random surroundings, and
check that none survives, and that ordinary diagnostic text passes through unchanged.

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
| 3. Dashboard | Overview with live updates, check page, alert log, settings | Done |
| 4. Eval checks | Statistics, result schema and storage, alert rules (floor, regression, not enough data, budget), eval page, JUnit and Stochast adapters | Done |
| 5. Summaries | Redaction | Done |
| | LLM summaries (Claude, OpenAI and others), evaluation of the summariser | Next |

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
