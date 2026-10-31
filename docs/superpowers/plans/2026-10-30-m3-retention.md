# M3 Retention Implementation Plan (M3-06, OR-01 / decision 115, DF-568, DF-572)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The org policy repo's `retention.yaml` sets how long each kind of data is kept (OR-01, owner decision 115). A scheduled control-plane job deletes what has expired, per tenant, and writes one audit event with the counts each time it runs. The audit chain is kept longest (7 years at least) and still verifies after its oldest records are pruned. A legal hold stops deletion for the scope it names. Trace capture (prompt and answer text in spans) is off unless a team opts in and the repo's classification allows it. The gateway's (TokenLean's) own retention gets the same periods through its configuration. Also: tenant deletion (DF-568) and invite mails that die with their invite (DF-572).

**Architecture:**
- **Policy (`agentic.policy.model`).** `retention.yaml` keeps its `days:` key (now a typed `RetentionDays`) and gains `legal_holds:`. Defaults are decision 115's: `audit` 2557 (7 years, also the floor), `runs` 400, `gateway_usage` 400 (TokenLean's billing floor), `traces` 30, `feedback` 365, `run_folders` 30, `logs` 30, `backups` 35. `null` keeps a kind forever. Validation: audit never below its floor and never shorter than any other period; gateway usage never below 400. `LegalHold(id, reason, data=[], teams=[])`: empty `data` is every kind, empty `teams` is the whole tenant. `TeamPolicy.trace_capture` (False) opts a team in; `ResolvedAgent.trace_capture` is the team's opt-in AND the classification's `trace_capture`; the native runner passes it to `model_instrumentation(include_content=...)`. `limits.yaml local.gc_after_days`, when not set, follows `days.run_folders` (`agentic gc`'s default becomes 30).
- **Control plane.**
  - `run_settings` gains `retention` (`RUN_SETTINGS_KEYS`; older versions are read again from their bundle, as before).
  - `agentic_cp/retention/`: `prune_tenant` (one tenant, one transaction: feedback, feedback journals, runs with their events, sent/failed mails, then the audit prefix) and `run_retention` (every tenant, under the `agentic.retention` session advisory lock, so several replicas or overlapping CronJob runs never prune at the same time). Runs as the **owner role**: the evidence tables stay insert-only for the runtime role (`db_roles.INSERT_ONLY` is unchanged).
  - The audit prune appends `retention.pruned` (counts per kind, the periods and holds in force, and the audit checkpoint `through_seq` / `through_hash`) and then deletes records `seq <= through_seq`. `audit.verify` accepts a chain whose first record is `seq N > 1` only when a `retention.pruned` record names `through_seq = N - 1` and `through_hash = first.prev`. The checkpoint lives inside the hash chain, so it cannot be edited unnoticed. Exports already anchor at the first record's `prev`.
  - `python -m agentic_cp retention run [--dry-run] [--tenant S]` and `retention gateway-config --tenant S` (prints the TokenLean `retention:` block for the tenant's periods; TokenLean has no admin API for retention).
  - Helm: `retention-cronjob.yaml` (daily, `concurrencyPolicy: Forbid`, the owner DSN), `retention.*` values; backups 35 days documented in the backup runbook and the chart README.
  - DF-572: `cancel_dead_invite_mails` marks a pending invite mail failed (body cleared, `notification.cancelled` audited) once its invite is revoked, used or expired; called by `revoke_unused` and at the start of every `deliver_job` (with or without a relay).
  - DF-568: `admin tenant delete --tenant S --confirm S` (owner role): only a suspended tenant without a legal hold; erases every row of the tenant but its audit chain, sets `status = 'deleted'`, audits `tenant.deleted` with counts. The retention run removes a deleted tenant's chain and its `tenants` row once its newest record is past the audit period. Migration `m3_06_retention` (revises `m3_01_roi`): the status check allows `deleted` (expand only).

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy Core + Alembic, Helm. No new dependency.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Scheduling | Kubernetes CronJob + the advisory-lock leader pattern of `policy_sync.worker` | APScheduler (MIT), pg_cron (PostgreSQL licence): a second scheduler, and pg_cron needs a server extension managed databases do not all offer |
| Deleting by age | SQLAlchemy Core `delete ... where key in (select ... limit n)` | pg_partman (PostgreSQL licence): partitions by time would change every evidence table's key and RLS; worth it only at much larger volume |
| Gateway retention | TokenLean's own `retention:` config (WS25) | writing TokenLean's tables from agentic is forbidden |
| Audit pruning proof | the chain's own `entry_hash` and the export's start anchor | RFC 3161 timestamping / transparency logs: an external anchor is a later hardening (DF) |

## Global Constraints

- Nothing in the runtime role's guarantees changes; retention and tenant deletion need `AGENTIC_CP_OWNER_DATABASE_URL`.
- A tenant without an active policy, or whose policy cannot be read, has nothing deleted.
- Every run that deletes writes `retention.pruned` on the tenant's chain in the same transaction as the deletes; `--dry-run` writes nothing.
- Expand-only migration; revises the current head `m3_01_roi`.
- No raw SQL outside the database layer (`test_no_raw_sql`).

## Interfaces

```python
# agentic.policy.model
AUDIT_FLOOR_DAYS = 2557; GATEWAY_USAGE_FLOOR_DAYS = 400
class RetentionDays(_Strict): audit: int | None; runs; gateway_usage; traces; feedback; logs; backups: int | None; run_folders: int
class LegalHold(_Strict): id: str; reason: str; data: list[DataKind] = []; teams: list[str] = []
OrgPolicy.retention_days: RetentionDays; OrgPolicy.legal_holds: list[LegalHold]
TeamPolicy.trace_capture: bool = False; ResolvedAgent.trace_capture: bool = False
model_instrumentation(*, include_content: bool = False)

# agentic_cp.retention
retention_settings(policy) -> dict
async prune_tenant(conn, stored: dict, now, *, dry_run=False, batch=10_000, policy_version=None, clock) -> PruneReport
async run_retention(owner, clock, *, dry_run=False, tenant=None, batch=10_000) -> dict[str, PruneReport | str] | None
gateway_config(stored) -> dict
async delete_tenant(owner, slug, clock) -> dict[str, int]
# agentic_cp.provisioning.invites
async cancel_dead_invite_mails(conn, *, now, clock, invite_ids=None) -> int
```

## TDD steps

- [ ] 1. Core policy: `RetentionDays` defaults and floors, `null`, legal holds (unknown kind, unknown team refused), template files load, legacy placeholder refused with the fix in the message; `gc_after_days` follows `run_folders`. (`core/tests/policy/test_retention_policy.py`)
- [ ] 2. Trace capture: off by default; team opt-in alone is not enough; both on → `include_content=True` in the native runner's instrumentation. (`core/tests/...`)
- [ ] 3. CP settings: `run_settings["retention"]`, key in `RUN_SETTINGS_KEYS`.
- [ ] 4. Audit verify with a checkpoint: pruned prefix with a checkpoint verifies; without one (or a wrong hash) it is reported. (`test_audit_chain.py`)
- [ ] 5. `prune_tenant`: expired runs+events, feedback, mails removed; young rows kept; audit kept longer than runs (a record older than the runs period survives); legal hold (whole tenant, by team, by kind) keeps rows; audit event with counts; dry run deletes nothing and writes nothing; chain verifies after prune; a policy-less tenant is untouched. (`control-plane/tests/test_retention.py`)
- [ ] 6. `run_retention` leader lock: a second runner while the lock is held returns None and deletes nothing.
- [ ] 7. CLI: `retention run --dry-run`, `retention gateway-config`.
- [ ] 8. DF-572: revoked and expired invite mails cancelled, body cleared, audited; a live one still goes out.
- [ ] 9. DF-568: migration; `admin tenant delete` refuses active and held tenants, erases all but audit, chain still verifies; retention removes the deleted tenant after the audit period.
- [ ] 10. Helm CronJob + test; docs (runbook `docs/runbooks/retention.md`, backup runbook, chart README, template comments); TRACKER, DEFERRED.

## Review Focus

1. **Audit kept longest and still verifiable** (row M3-06). Floors in the model; prune leaves a checkpoint inside the chain; `verify` and `db verify-restore` pass after a prune and fail on an unanchored gap. Tests: steps 4 and 5.
2. **Expired rows removed, nothing else.** Young rows, open runs inside the period, held scopes, pending mails and a policy-less tenant are untouched. Step 5.
3. **Several replicas.** The advisory lock and `concurrencyPolicy: Forbid`. Step 6.
4. **The runtime role stays insert-only on evidence.** Only the owner role deletes; `enforcement_status` is unchanged.
5. **Tenant deletion is irreversible**: suspended first, confirmation by slug, legal hold refuses, audit chain survives.
