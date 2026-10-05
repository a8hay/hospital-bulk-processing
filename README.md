# Hospital Bulk Processing

- **Live app:** https://hospital-bulk-8g2n.onrender.com (interactive docs at [`/docs`](https://hospital-bulk-8g2n.onrender.com/docs))
- **Repository:** https://github.com/a8hay/hospital-bulk-processing

Both the app and the upstream API run on Render's free tier and sleep when idle, so the first
request after a quiet period can take 30–60s.

A service that accepts a CSV of up to 20 hospitals, creates each one through the
[Hospital Directory API](https://hospital-directory.onrender.com/docs), and activates the batch once
every row exists. Processing is asynchronous: the upload returns `202 Accepted` immediately and the
client polls for progress. A failed or interrupted batch can be resumed without creating duplicates.

## Quick start

```bash
docker compose up --build        # app on http://localhost:8000, Postgres on localhost:5434
open http://localhost:8000/docs  # interactive API docs
```

```bash
curl -i -F "file=@hospitals.csv;type=text/csv" localhost:8000/hospitals/bulk
# HTTP/1.1 202 Accepted
# location: /hospitals/bulk/67d4a3ad-...

curl localhost:8000/hospitals/bulk/67d4a3ad-...   # poll until status is no longer processing/activating
```

To run the app outside Docker, start only the database and use the example environment:

```bash
docker compose up -d db
cp .env.example .env
uv run uvicorn --factory app.main:create_app --reload
```

## API

| Endpoint | Purpose | Responses |
| --- | --- | --- |
| `POST /hospitals/bulk` | Upload a CSV and start processing | `202` + `Location`; `400` invalid CSV (every error, with row and line); `413` too large |
| `GET /hospitals/bulk/{batch_id}` | Progress and final result | `200`; `404` |
| `POST /hospitals/bulk/{batch_id}/resume` | Continue a `failed`, `interrupted` or `activation_failed` batch | `202`; `404`; `409` if running, completed, or has rejected rows |
| `POST /hospitals/bulk/validate` | Check a CSV without processing it | `200` with `valid` and `errors` |
| `GET /health` | Liveness | `200` |

**CSV contract.** The header must be exactly `name,address,phone`. `phone` values may be empty and
are accepted in any format, as the upstream does. Cells are trimmed, blank lines are skipped, an
Excel BOM is tolerated. Validation is all-or-nothing: one bad row rejects the file, and the response
lists every problem so it can be fixed in one pass.

**Result shape.** `GET` returns the response shape from the spec (`batch_id`, `total_hospitals`,
`processed_hospitals`, `failed_hospitals`, `processing_time_seconds`, `batch_activated`,
`hospitals[]`), plus `status`, `pending_hospitals` and `error`, which an asynchronous job needs.

## How it works

```
POST /bulk ──validate──▶ insert batch (processing) + rows (pending) ──▶ 202
                                     │
                              runner task (in-process)
                                     │
        warm-up GET ─▶ pass: POST each pending row (concurrent, capped)
                          │      ├─ 2xx            → created
                          │      ├─ 4xx            → rejected (terminal)
                          │      ├─ connect / 429  → retry with jittered backoff
                          │      └─ read timeout / 5xx → unknown
                          ▼
                     reconcile unknown rows against GET /hospitals/batch/{id}
                          │  (found → adopt its id; absent → safe to send again)
                          ▼
              all created? ─▶ PATCH activate ─▶ completed
                    no     ─▶ failed (resumable unless a row was rejected)
```

### The rule everything follows

**Never send a request whose effect may already exist upstream.** The upstream has no idempotency
key and no deduplication: the same hospital POSTed twice is created twice. Activation is not
idempotent either: a second `PATCH activate` returns `400 already active`. So:

- A failure that proves the request never arrived (connection refused, connect timeout, pool
  timeout, 429) is retried.
- A failure that might have been processed (read timeout, dropped connection, 5xx, or a 2xx whose
  body can't be read) makes the row `unknown`. Before anything is re-sent, the runner lists the
  upstream batch and adopts any hospital that matches. Rows may legitimately be identical, so
  matching is done as a multiset that excludes ids already owned.
- An ambiguous activation is verified by listing the batch, not by activating again.

### State

Postgres holds every batch and row, so progress survives restarts and resume has something to resume
from. Every transition is a guarded `UPDATE ... WHERE status = <expected>`, so a writer that lost a
race changes nothing.

| Batch status | Meaning |
| --- | --- |
| `processing` | Rows are being created |
| `activating` | Every row created; activating upstream |
| `completed` | Activated |
| `failed` | Some rows not created; batch left inactive |
| `activation_failed` | Rows created but activation did not succeed |
| `interrupted` | The runner stopped (crash, redeploy, shutdown) |

Row statuses: `pending`, `in_flight`, `created`, `unknown`, `retry_exhausted`, `rejected`. The
database enforces that a row is `created` exactly when its upstream hospital id is known.

### Crash recovery and resume

- The runner writes a heartbeat every 5s. A sweeper (on startup and every 30s) marks batches whose
  heartbeat is older than 30s as `interrupted`, and moves their `in_flight` rows to `unknown` in the
  same statement. Their outcome is uncertain, so they will be reconciled, never blindly re-sent.
- Resume is a single compare-and-set `UPDATE`. Of two simultaneous resume requests, the second
  blocks on the row lock, re-evaluates its `WHERE` against the committed row, and matches nothing,
  so it gets a `409`. Rows that ran out of retries get a fresh budget; `unknown` rows are reconciled
  first.
- A batch with a `rejected` row cannot be resumed: the upstream refused the data, so retrying
  cannot help. Fix the data and upload a new file.

## Upstream behaviour (measured, not assumed)

| Observation | Consequence |
| --- | --- |
| POST takes 5.5–7.5s, but 20 concurrent POSTs finish in ~7.5s total | Concurrency, not sequential calls; read timeout 30s |
| Cold start after idle: 27.6s | A warm-up `GET /` (60s timeout) runs before any write |
| Same row POSTed twice creates two hospitals | Reconcile before re-sending |
| Second `PATCH activate` returns 400 | Verify activation by listing, never repeat blindly |
| A batch is capped at 20, but 35 concurrent POSTs all got in | Enforce the limit ourselves at upload |
| A 30-per-minute rate limit exists (no `Retry-After` header) | 429 is retryable; `Retry-After` is honoured if ever sent |
| Ids reset after a cold start | Upstream data is not durable; an activate 404 is reported, not retried forever |

## Design decisions and trade-offs

| Decision | Why | Trade-off accepted |
| --- | --- | --- |
| Async processing (`202` + polling) | The job must outlive the HTTP connection: a client disconnect must not leave a half-created batch whose id was never returned | The client polls; a WebSocket push is not implemented |
| Postgres, not in-memory | Resume after a crash needs state that survives the process | One more piece of infrastructure |
| One gunicorn worker, in-process runner and semaphore | The work is I/O-bound; one event loop holds hundreds of in-flight calls, and the hosting tier has under one CPU | Horizontal scaling would need a shared concurrency limit and a job queue |
| Concurrency cap of 10 shared by every upstream call | The upstream rate-limits per server, not per endpoint | Tunable by `UPSTREAM_CONCURRENCY` |
| No fencing token on runner writes | Single process; a swept runner stops at its next heartbeat or row claim | A stalled runner could complete one in-flight write after being swept; reconciliation absorbs it |
| `processing_time_seconds` is wall-clock from first start | It is what the client experiences | Includes idle time between a failure and a resume |
| Failed batches leave inactive hospitals upstream | Deleting them would destroy what resume needs | Abandoned batches leave inactive orphans |
| Schema applied with `CREATE ... IF NOT EXISTS` at startup | Two tables; a migration tool would be more ceremony than value | No history of schema changes |

## Configuration

All settings are environment variables (see `app/config.py`). The important ones:

| Variable | Default |
| --- | --- |
| `DATABASE_URL` | **Required**, no default, so a missing value fails at startup. `postgres://` URLs are accepted. On Render it is injected from the managed database |
| `UPSTREAM_BASE_URL` | `https://hospital-directory.onrender.com` |
| `UPSTREAM_CONCURRENCY` | `10` |
| `UPSTREAM_READ_TIMEOUT` / `UPSTREAM_WARM_UP_TIMEOUT` | `30` / `60` seconds |
| `MAX_ROWS` / `MAX_UPLOAD_BYTES` | `20` / `1000000` |
| `MAX_ATTEMPTS` / `MAX_PASSES` | `4` / `3` |

## Tests

```bash
docker compose up -d db   # integration tests need Postgres (database hospital_bulk_test)
uv run pytest
```

- `tests/unit`: CSV validation, outcome classification, reconciliation matching, backoff. No I/O.
- `tests/integration`: the runner, repository and HTTP API against real Postgres, with the upstream
  replaced at the httpx transport by a small stateful fake that can commit a write and then lose the
  response. Includes concurrent resume, crash-and-resume, and every ambiguous-failure path.

The duplicate-prevention tests were checked by deliberately making read timeouts retryable: exactly
the five tests that guard the rule fail.

## Deployment

`render.yaml` defines a Docker web service and a managed Postgres for Render. The service reads
`DATABASE_URL` from the database. Note that Render's free Postgres instances expire, and the free web
service sleeps when idle, so the first request after idle is slow.

## Project layout

```
app/
  main.py             app factory, lifespan, upload size middleware
  config.py           settings from the environment
  api/                routes and our API's schemas
  domain/             pure logic: models, CSV validation, reconciliation, backoff (imports nothing else)
  integration/        upstream client: one HTTP exchange in, one classified outcome out
  persistence/        schema.sql and every SQL statement
  runner.py           drives a batch to a terminal state; knows nothing about FastAPI
  sweeper.py          marks batches whose runner died as interrupted
```
