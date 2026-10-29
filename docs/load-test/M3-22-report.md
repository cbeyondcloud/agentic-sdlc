# M3-22 Load test report: 1000 concurrent runs

## Summary

- **1000 concurrent runs met every proposed target** on two 1-vCPU control-plane replicas:
  0.001 % errors (1 of 104,257 requests), p95 ingest lag 0.70 s from the CLI producing an
  event to it being committed and readable, kill-switch poll p99 0.67 s, heartbeat p99
  1.04 s, registration p95 0.78 s. Postgres used a quarter of a CPU.
- **It did not at first.** At only 200 runs, 43 % of requests timed out. The load test found
  five bottlenecks in our code and fixed them, each with a test: registration checks run
  under a tenant-wide lock, `/v1/me` costing 318 ms of CPU (DF-406), a database pool that
  logged in again and again, a write on every request to record a token's last use, and a
  transaction on every kill-switch poll (DF-704).
- **Headroom is thin with two replicas:** each averaged 55 % CPU with bursts to 100 %. A
  stress run at four times the event rate, with runs starting every 4 minutes per person,
  saturated even three replicas (3.3 % errors, p95 lag 13 s, no event lost). Capacity
  follows how often runs start and how fast they produce events. Recommended sizing:
  **3 replicas x 1 vCPU / 1 GB**, scaled out on CPU (master plan "Sizing" updated); Postgres
  as planned.
- **Indicative, not production sizing:** one Windows laptop whose Docker VM also ran other
  projects' containers. Re-run on staging (list at the end).

## What ran

### The machine

One Windows laptop, so the numbers are **indicative, not production sizing**:

- Intel Core i5-11400H (6 cores, 12 threads), 32 GB RAM, Windows 11.
- Docker Desktop on WSL2 with a Linux VM of 6 CPUs and 16 GB.
- The VM also ran about 25 containers of other projects (Postgres, Keycloak, CI jobs),
  some using 20-30 % of a CPU each during the runs. The load driver ran natively on the
  same laptop (Python 3.12, Windows event loop).

### The target stack

`control-plane/loadtest/compose.yml`, project `agentic-m3-load-test`, built from this
branch's `control-plane/Dockerfile`:

| Container | What | Limits |
|---|---|---|
| `lt-postgres` | `postgres:17.10-alpine` (pinned by digest), fsync and synchronous commit on, `shared_buffers=1GB`, `max_connections=300`, `pg_stat_statements` | 2 CPU, 4 GB |
| `lt-cp` x 2 | `python -m agentic_cp serve` (one uvicorn process each) | 1 CPU, 1 GB each |
| `lt-worker` | `python -m agentic_cp worker` (zombie sweeps, outbox, auto-trip) | 0.5 CPU, 512 MB |

The master plan sizes production at 2-3 replicas x 1 vCPU / 1 GB and Postgres at 4 vCPU /
16 GB. Postgres here had half that. Runs were spread over the replicas by run, as a
Kubernetes Service spreads connections.

### The load

`control-plane/loadtest/` (see its README). Each simulated run speaks the CLI's protocol:
the async client is held to `agentic_cli.control_plane.ControlPlaneClient` by a contract
test, and batches use the CLI sink's own chunking. Events are a recorded feature-delivery
ledger (40 events: 14 step executions, 4 gate waits, 2 loop-backs), chained again for each
run with the ledger's hash, and verified by the control plane as real batches are.

| Per run | Rate |
|---|---|
| first run of a person: `/me`, `/policy/active`, bundle | once |
| each run: `/policy/active`, registration | once |
| heartbeat | every 60 s while the CLI runs (policy default) |
| kill-switch poll, with ETag | every 10 s while the CLI runs (CLI cache) |
| events | 40 per run; a step lasts 60 s +/- 50 %, a gate 30 s +/- 50 % |

Both durations are pessimistic: real agent steps take minutes and real gates wait longer.
A closed run is followed by the same person's next run, so 1000 stay open. People: 2
tenants x 10 teams x 50 people, each with `developer` in their team and their own token.

### What was measured

- **Ingest lag, end to end:** from the simulated CLI appending an event to (a) the ingest
  response that carried it (`commit`, includes client queueing, retries and catch-up) and
  (b) a separate superuser connection first reading the run's head at or past it
  (`visible`, read every 250 ms - the resolution of that number).
- Latency and statuses per route; errors by status (timeouts, transport errors, 5xx, 429
  and 4xx; the kill poll's 304 is not an error).
- Postgres: connections by state, lock waits, commits/s, sessions opened, and the 15
  costliest statements (`pg_stat_statements`).
- CPU and memory of every container (`docker stats`, every 5 s).
- The driver's own event-loop lag.

## Bottlenecks found and fixed

Found with 200-run warm-ups on the same stack, in this order. Each fix has a test that
failed before it.

| # | What the run showed | Cause | Fix (commit) |
|---|---|---|---|
| 1 | 43 % of requests timed out at only 200 runs; `pg_advisory_xact_lock` averaged 620 ms (328 s in all); up to 28 connections idle in transaction | Registration ran **every** org check under the tenant's registration lock. Queued registrations each held a pool connection, so kill polls and heartbeats waited for a connection and timed out. Client timeouts then left orphan registrations (336 open runs for 200 slots) | Checks run first; the lock now guards a second concurrency count and the insert only, so two at cap-1 still get one slot (R8). `25a61f1` |
| 2 | `GET /v1/me` cost about 318 ms of CPU per call (cProfile in the container); 34 of the profile's 35.5 s in casbin | `effective_permissions` asked casbin about every permission in every scope (about 100 matcher runs over every policy line). The CLI calls `/me` on `agentic init` and again whenever the active policy changes, so one policy change makes 1000 CLIs cost about 320 CPU-seconds at once (DF-406) | Permissions read from casbin's role manager once per scope; a parity test compares the answer with one decision per permission for 60 people. `8ee2a09` |
| 3 | 1210 database sessions opened in under an hour; about 4096 HMAC rounds per login in the profile | The pool (5 + 10 overflow) closed every overflow connection it took back, so a busy replica logged in to Postgres again and again (SCRAM in Python, a new backend each time) | `AGENTIC_CP_DB_POOL_SIZE` (15) and `AGENTIC_CP_DB_MAX_OVERFLOW` (0): the same ceiling, kept open. `26874c0` |
| 4 | `UPDATE tokens SET last_used_at` on every request (6445 calls in one warm-up) | A transaction per request to record a token's last use, which changes at most once a minute | Skipped while this process wrote it within the minute. `f6f5eb6` |
| 5 | Two thirds of all requests were kill polls, each a tenant transaction | The poll read the tenant's switches every time, though they almost never change (DF-704) | A process reuses a tenant's switches for `AGENTIC_CP_KILL_CACHE_SECONDS` (2 s); its own changes show at once. A kill still lands inside the 30 s target (10 s CLI cache + 5 s watchdog + 2 s); the propagation E2E still stops runs in about 10 s. `0a1b841` |
| - | Events looked visible before they were sent | Harness bug: the head watcher matched run ids across tenants, and two load tests reused ids | Keyed by tenant and run; ids carry a per-test tag. `e14f3ea` |

After fixes 1 and 2 the same 200-run warm-up went from 43 % errors and a p95 commit lag of
24.7 s to 0.6 % errors and 4.1 s, with the replicas still near their CPU limit; fixes 3 to 5
took them to the 1000-run result below. (Commit lag is quoted here: the warm-ups' visible lag
was affected by the harness bug in the last row.)

**Seen but not changed:**

- **The tenant's audit chain lock.** A gate-waiting event queues approver mail, and queuing
  mail appends to the tenant's audit chain under a per-tenant lock held until the ingest
  commits (386 lock waits in one warm-up, 97 ms mean). One hash chain per tenant needs that
  lock; it hurts only when replicas are short of CPU and hold it longer. If it shows on
  staging, append the audit records in one step right before commit, or chain mail records
  in the outbox worker.
- **CPU per request.** What is left is spread over FastAPI, SQLAlchemy and asyncpg (about
  12 database round trips per request: a pre-ping and the tenant checks on each
  transaction). The image's uvicorn runs on the pure-Python loop and HTTP parser
  (`asyncio` + `h11`); `uvloop` and `httptools` (MIT/Apache-2.0, Linux only) would save
  roughly 15 % of the busy time measured. Not added here: a new dependency for the owner to
  decide.

## Results at 1000 runs

Reference run, 2026-10-29, image built from `0a1b841` (every fix above), fresh database.
2 tenants x 10 teams x 50 people, 120 s ramp, 600 s steady, default timing (step 60 s,
gate 30 s, beat 60 s, kill poll 10 s). Raw summary:
[`M3-22-run-1000.json`](M3-22-run-1000.json).

**Every proposed target was met.**

| Measure | Result | Target |
|---|---|---|
| Requests | 104,257 in 721 s (about 145/s) | - |
| Errors | 1 (one transport error on an ingest, retried by the sink): 0.001 % | <= 0.1 % |
| Ingest lag, visible: p50 / p95 / p99 | 0.19 s / **0.70 s** / 1.48 s | p95 <= 5 s, p99 <= 15 s |
| Ingest lag, commit: p50 / p95 / p99 | 0.03 s / 0.59 s / 1.38 s | - |
| Events produced / committed / visible | 27,376 / 27,376 / 27,376; none left undelivered | - |
| Kill-switch poll p99 | 0.67 s (58,560 polls, 93 % answered 304) | < 2 s |
| Heartbeat p99 | 1.04 s (14,441 beats) | < 2 s |
| Registration p95 | 0.78 s (1000 registrations, all passed every check) | <= 2 s |

Latency per route (ms):

| Route | Count | p50 | p95 | p99 |
|---|---|---|---|---|
| `GET /v1/killswitch/state` | 58,560 | 23 | 225 | 665 |
| `POST /v1/runs/{id}/events` | 27,256 | 36 | 422 | 947 |
| `POST /v1/runs/{id}/heartbeat` | 14,441 | 41 | 453 | 1042 |
| `POST /v1/runs` | 1,000 | 32 | 781 | 1280 |
| `GET /v1/me` (first run only) | 1,000 | 32 | 1012 | 1674 |
| `GET /v1/policy/active` | 1,000 | 16 | 626 | 1289 |
| bundle | 1,000 | 16 | 643 | 1038 |

The slow tails of `/me`, `/policy/active`, the bundle and registration are the ramp: 1000
people starting in 120 s on top of the running load.

Resources:

| Container | CPU avg | CPU p95 | CPU max | Memory max |
|---|---|---|---|---|
| `lt-cp-1` (limit 1 CPU, 1 GB) | 55 % | 96 % | 101 % | 126 MB |
| `lt-cp-2` (limit 1 CPU, 1 GB) | 56 % | 98 % | 100 % | 123 MB |
| `lt-postgres` (limit 2 CPU, 4 GB) | 24 % | 44 % | 53 % | 251 MB |
| `lt-worker` (limit 0.5 CPU) | 3 % | 25 % | 38 % | 105 MB |

CPU is in % of one core. Postgres: at most 34 connections (2 x 15 and the test's own), on
average 1.4 active, at most 2 sessions waiting on a lock, 237 commits/s, 171 sessions opened
during the whole run. The driver's own event loop was late by 22 ms at p99 (Windows timer
resolution is about 15 ms), so it did not distort the numbers.

**Not covered by this run:** each run lasts about 16 minutes at this timing, longer than the
test, so runs did not close and register again during the steady state (the 1000
registrations all came in the ramp). The stress run below covers that.

### Stress run: three replicas, four times the event rate

Same stack with 3 replicas, 1000 runs, but a step of 15 s and a gate of 10 s: four times
the events, and runs close and register again all through the test (1938 registrations,
966 runs closed in 7 minutes - each person starting a run every 4 minutes). 120 s ramp,
300 s steady. Raw summary:
[`M3-22-stress-1000x4-3replicas.json`](M3-22-stress-1000x4-3replicas.json).

**It saturated the replicas and missed the targets:** 87,696 requests, 3.3 % errors (all
timeouts: kill polls and heartbeats at their 5 s limit), ingest lag p95 13.2 s and p99
16.3 s, kill poll p99 7.5 s, registration p95 8.0 s. No event was lost: all 49,932 were
committed and visible by the end (the sinks retried). Each replica averaged 89 % CPU (p95
100 %); Postgres 57 % of a CPU and 473 MB.

The requests that cost the most CPU are registrations and ingests (every org check; event
verification, gate mails and their audit records), not kill polls: this run sent only 26 %
more requests per second than the reference run but needed 2.4 times the CPU. So capacity
follows **how often runs start and how fast they produce events**, not just how many are
open.

## Sizing

| | Evidence (this laptop) | Recommendation |
|---|---|---|
| Control plane | 2 x 1 vCPU met every target at 1000 runs with 55 % average CPU and bursts to 100 %; 3 x 1 vCPU saturated at four times the event rate | **3 replicas x 1 vCPU / 1 GB** for 1000 concurrent runs: the third is headroom and keeps the targets through a rolling deploy or a lost node. Scale out on CPU (target about 60 %) when runs start more often than about one per person per 15 minutes. Memory: under 160 MB used per replica, so 1 GB is ample |
| Database pool | 15 connections per replica kept open; at most 34 in use with 2 replicas | `AGENTIC_CP_DB_POOL_SIZE` 15 (default); Postgres `max_connections` at least replicas x 15 + worker + Grafana + operators |
| Postgres | 0.24 vCPU average, 0.53 max and 251 MB at the reference load; 0.57 vCPU and 473 MB under stress | **4 vCPU / 16 GB with an HA standby, as planned**: the run used a fraction; the size is for months of `run_events` and `audit_log`, the dashboards' queries and the standby's synchronous commits, none of which this test measured |

The master plan's "Sizing" section is updated to this.

## Targets (proposed; owner decision pending)

| Measure | Target | What fails when it is missed |
|---|---|---|
| Ingest lag (visible), p95 | <= 5 s | Dashboards, budget alerts and gate-waiting mails arrive late; the stale-run sweep sees old `last_event_at` |
| Ingest lag (visible), p99 | <= 15 s | As above for the slowest runs; a backlog that grows instead of draining |
| Error rate, all routes | <= 0.1 % | Failed ingests are retried (no loss) but pile up in the offline queue; failed beats make live runs look stale |
| Kill-switch poll, p99 | < 2 s | The CLI waits 5 s at most; after 60 s without a confirmed answer runs pause, and a kill takes longer than the 30 s target (M2-54) |
| Heartbeat, p99 | < 2 s | A beat times out at 5 s; missed beats free a live run's slot |
| Registration, p95 | <= 2 s | Every `agentic run` waits on it |

## Deferred items measured

| DF | What the load test says |
|---|---|
| DF-406 (effective permissions scan) | Confirmed and fixed (fix 2). `GET /v1/me` at 1000 runs: p50 32 ms, p95 1.0 s during the ramp. |
| DF-704 (a 304 kill poll still does the database work) | Confirmed and fixed (fix 5): the poll was two thirds of all requests. |
| DF-569 (bulk role import) | Seeding imports 500 assignments per tenant through `POST /v1/rbac/import`: 9.0 s and 8.7 s (about 18 ms a row), with RBAC changes in that tenant waiting meanwhile. Fine for an onboarding batch of 500; a 5000-row import would hold the lock for about 90 s, so the preload fix stays worth doing before a big org imports in one file. |
| DF-487 (conditional GET for bundles) | 13.4 MB of bundles for 1000 first runs (13.4 KB each). Not a bottleneck at this policy size; revisit if policy repos grow large. |
| DF-609 (serial metrics refresh) | One `/metrics` scrape in the middle of the run took 0.04 s with 2 tenants. The cost grows with tenants, not runs; re-check on staging with the real scrape interval and tenant count. |
| DF-401, DF-485, DF-489, DF-492 | Policy sync is not on the run path and was not loaded here (the worker's polling was slowed down). |
| DF-769/DF-776, DF-773, DF-774 | Outage and CLI heartbeat behaviour, not load: unchanged. |

## What to re-run on staging

Staging (M3-04) is the first place where the numbers mean production. Run, from a machine
next to it (see `control-plane/loadtest/README.md`, "Against another deployment"):

1. **The same 1000-run test** at the master plan's sizing (2 replicas x 1 vCPU / 1 GB,
   Postgres 4 vCPU / 16 GB with its HA standby, so commits wait for the standby):
   `run --runs 1000 --tenants 1 --teams 20 --ramp 120 --duration 1800`. One tenant is the
   real shape of one org and the worst case for the per-tenant locks (registration, audit
   chain). Thirty minutes lets runs finish and new ones register.
2. **Headroom:** the same with `--runs 2000`, and with 3 replicas, to find where the
   targets break and what one more replica buys.
3. **A policy change under load:** activate a new policy version in the middle of a run.
   Every CLI's next run then fetches `/me` and the bundle; watch CPU and `/me` latency
   (fix 2 is what makes this affordable).
4. **Through the real ingress** (TLS, the ingress controller's own limits and timeouts),
   with Prometheus scraping `/metrics` every 15-30 s on every replica (DF-609).
5. **Long run:** 8 hours at 1000 runs, to see table growth (`run_events`, `audit_log`,
   `notifications`) and whether ingest, the stale-run sweep and the dashboards' queries
   stay flat. Then `VACUUM`/autovacuum behaviour and the audit export range lookup
   (DF-607) on that volume.
6. **An outage drill:** stop Postgres for longer than `stale_minutes` during the run and
   check that the CLIs' queues drain without loss afterwards (DF-769/DF-776).
