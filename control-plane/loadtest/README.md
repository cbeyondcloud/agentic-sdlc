# Control-plane load test (M3-22)

Simulated CLIs drive feature-delivery runs against a real control plane and measure what
1000 people running at once would cost it. The results of the reference run are in
[`docs/load-test/M3-22-report.md`](../../docs/load-test/M3-22-report.md).

## What a simulated run sends

Each run speaks the CLI's own protocol (`cp_loadtest/wire.py` is held to
`agentic_cli.control_plane.ControlPlaneClient` by a contract test):

| When | Request | Rate per run |
|---|---|---|
| a person's first run (`agentic init`) | `GET /v1/me`, `GET /v1/policy/active`, the bundle | once |
| every run | `GET /v1/policy/active`, `POST /v1/runs` (every org check) | once |
| while the CLI runs | `POST /v1/runs/{id}/heartbeat` | every `--heartbeat-s` (policy default 60 s) |
| while the CLI runs | `GET /v1/killswitch/state` with the last ETag | every `--kill-poll-s` (CLI cache, 10 s) |
| each event | `POST /v1/runs/{id}/events` (background sink, CLI chunking) | 40 events per run |
| at a gate | one `waiting` beat; then nothing until a person answers | 4 gate waits per run |

The events are a recorded feature-delivery ledger (`fixtures/feature-delivery.events.jsonl`:
14 step executions, 4 gate waits, 2 loop-backs), chained again for each run with the
ledger's own hash. A step lasts `--step-s` (60 s) and a person answers after `--gate-s`
(30 s), each with +/- `--jitter` (50 %). Both are short on purpose: real steps take minutes
and real gates wait longer, so this is a pessimistic load.

## What it measures

- **Ingest lag, end to end.** From the moment the simulated CLI appends an event to when
  the ingest that carried it returned 2xx (`commit`), and to when a separate database
  connection first read the run's head at or past it (`visible`, read every 250 ms).
- Latency per route (p50/p95/p99/max), statuses, errors by status. An error is a timeout,
  a transport error, any 5xx, a 429 or any 4xx; the kill poll's 304 is not.
- Postgres connections by state and lock waits; commits per second.
- CPU and memory of every container of the stack (`docker stats`).
- The driver's own event-loop lag (a busy driver would inflate what it measures).

## Run it

Needs Docker or Podman (`CONTAINER_ENGINE` picks one) and `uv`. The same commands work in
PowerShell, bash and zsh:

```
uv run python control-plane/loadtest/loadtest.py up --replicas 2
uv run python control-plane/loadtest/loadtest.py run --runs 1000 --duration 600 --ramp 120
uv run python control-plane/loadtest/loadtest.py down
```

- `up` builds `agentic-control-plane:m3-load-test` from `control-plane/Dockerfile` and starts
  the compose project `agentic-m3-load-test`: Postgres (2 CPU / 4 GB by default), N
  control-plane replicas (1 CPU / 1 GB each) and the worker. `LT_PG_CPUS`, `LT_PG_MEMORY`,
  `LT_CP_CPUS` and `LT_CP_MEMORY` change the limits. Ports bind to 127.0.0.1 only.
- `run` seeds fresh tenants (`--tenants`, `--teams` per tenant, one person per run, each
  with `developer` in their team), drives the runs, prints the summary and writes it as JSON
  (`--out`, default `.state/last-run.json`). It exits 3 when a target is missed.
- `down` removes the project's containers and its volume. The image stays; remove it with
  `docker image rm agentic-control-plane:m3-load-test`. If compose fails, `down` exits
  non-zero and keeps `.state/stack.json` (the volume still has those passwords); run it
  again once the engine is back. It also works without the state file, to clear leftovers.

**Against another deployment** (staging): set `LT_BASES` (the replicas' URLs, comma
separated; plain http to 127.0.0.1 or https), `LT_OWNER_DSN` and `LT_APP_DSN` (the owner and
runtime roles, for seeding), `LT_ADMIN_DSN` (a role that reads `runs` across tenants, for the
head watcher and `pg_stat_activity`) and optionally `LT_METRICS_TOKEN`, then use `run` from a
machine next to it, without `up`. Seeded tenants stay; their policy source is a `file://`
path on the runner, which the deployment's worker cannot sync later (it logs and backs off).
Container CPU and memory then come from the platform's own monitoring.

## Targets (proposed, owner decision pending)

| Measure | Target |
|---|---|
| Ingest lag (visible), p95 / p99 | <= 5 s / <= 15 s |
| Error rate | <= 0.1 % |
| Kill-switch poll p99, heartbeat p99 | < 2 s |
| Registration p95 | <= 2 s |

The smoke version (`control-plane/tests/loadtest/test_loadtest_smoke.py`, 20 runs for 30 s
with compressed timing against the app under uvicorn) runs in `scripts/check.py`.
