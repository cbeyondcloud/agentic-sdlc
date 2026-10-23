# SP15 Slice 2b: Notifications, Service Accounts, Provisioning and the RBAC CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The control plane mails people through the org's SMTP relay (a durable outbox: never lost, never queued twice, retried with backoff, STARTTLS by default): a gate waiting for them, a run or team budget crossing 80 % and 100 % (once per crossing, never again on replay; a webhook seam beside the mail), and an invite. An admin creates team-scoped service accounts that can only trigger runs (an expiring token shown once) and bulk-imports role assignments from a CSV file (dry-run diff first, idempotent, per-row errors with line numbers). Assigning a new person a role sends them a single-use, expiring invite that `agentic login --invite` redeems once for a control-plane token and a gateway (TokenLean) proxy key; offboarding revokes both, holds their running work and is audited. Users rotate their own proxy key; a revoked key makes the CLI pause the run with "run `agentic login`" instead of a raw HTTP error. Admins do all of it from `agentic rbac`.

**Rows:** M2-07, M2-14, M2-16, M2-17, M2-18, M2-25 (the `agentic rbac` part). SP15 slice 2b.

**Architecture:**
- **Outbox (M2-07).** Table `notifications` (tenant-owned, forced RLS): one row per mail, `UNIQUE (tenant_id, dedupe_key)`, written with `ON CONFLICT DO NOTHING` in the same transaction as the event that causes it (`notify.enqueue`). `notify.templates` renders plain text plus a simple HTML part from package-data templates (`string.Template`, every value HTML-escaped; no images, no remote content, no tracking). `notify.smtp.SmtpNotifier` sends through stdlib `smtplib` on a worker thread (`starttls` default, `tls` implicit, `none` only by explicit setting). `notify.delivery.deliver_due(engine, tenant_id, notifier, clock)` is a per-tenant worker job: each due row is locked (`FOR UPDATE SKIP LOCKED`), sent while locked, marked `sent` (bodies cleared) or rescheduled with exponential backoff, or `failed` after a permanent refusal or the last attempt; the Message-ID is derived from the row id, so a resend after a crash between the relay's accept and the commit is recognisable. Every state change is on the tenant's audit chain (`notification.queued` / `.sent` / `.failed`, never the body).
- **Budget alerts (M2-16).** `RunRegistration` gains `budget_usd`; `runs` gains `budget_usd` and `spent_usd`. Ingest adds each new `step_finished.cost.usd` (duplicates are not re-counted: ingest already ignores replayed entries) and calls `alerts.budget.check_run` / `check_team`: 80 % and 100 % of the run budget and of the team's monthly budget (`teams/<t>.yaml: monthly_budget_usd`, kept in `policy_versions.settings.team_budgets`), team spend being the sum over the team's runs opened this UTC month. Each crossing raises one `BudgetAlert` to every `AlertSink`: the outbox (dedupe key `budget:<scope>:<id>:<period>:<threshold>`) and the `WebhookAlertSink` protocol (interface only, `NoWebhook`). Gate waiting: `GateLedger.waiting` records the gate's approver role; ingest of `gate_waiting` mails the run's starter and the people assigned that role in the run's team.
- **Service accounts and CSV import (M2-14).** `POST /v1/service-accounts {name, team, days}` (days required, 1..365, decision 75) creates `users.kind = service` with the only role it may ever hold (`service-account` at team scope) and returns the token once; `GET /v1/service-accounts`. Defence in depth: `authorize` grants a service principal nothing outside `SERVICE_PERMISSIONS = {"run.trigger"}`, and `PUT /v1/rbac/roles/service-account` cannot widen it. `POST /v1/rbac/import {csv, dry_run}`: `rbac.importer.parse` (BOM, CRLF, physical line numbers) and `plan_import` (per-row authorisation like a single assignment); applying is all or nothing and additive only. Operator commands `admin tenant suspend|resume` (DF-408).
- **Proxy keys (M2-17).** `keys.gateway.GatewayKeys` port (`create`, `revoke`, `rotate`) with `TokenLeanKeys` (httpx2 against TokenLean's `/admin/keys`, ST-9) and `NoGatewayKeys`; tests use a fake. Table `proxy_keys` records which TokenLean key (`key_id`) belongs to which user. `POST /v1/proxy-keys/rotate` (the caller's own key, new raw key once) and `DELETE /v1/users/{email}/proxy-keys` (admins). Core: `agentic.transport.GatewayKeyRejected`, raised by the model client on a gateway 401; the engine pauses the run `gateway-key` (resumable); the CLI's next step says to run `agentic login` and resume. `agentic login` verifies a gateway key with `GET <gateway>/v1/models` (DF-182) and `agentic login --rotate` rotates it through the control plane.
- **Provisioning (M2-18).** Table `invites` (`token_sha256` unique across tenants) and the registered definer function `resolve_invite(bytea)`. A new human user's first assignment (single or imported) creates an invite and queues the invite mail; `POST /v1/users/{email}/invite` re-sends (earlier unused invites revoked). `POST /v1/invites/redeem {token}` (no bearer; the tenant comes from the invite) consumes the invite with one conditional UPDATE and returns a 90-day control-plane token, the proxy key (when the deployment has a gateway admin and the tenant a gateway tenant) and the URLs. `POST /v1/users/{email}/offboard {reason}`: user disabled, tokens and unused invites revoked, a `user:<email>` kill switch set, proxy keys revoked through the port (a gateway failure leaves them `revoke_requested`, retried by the worker).
- **CLI (M2-25 rbac).** `agentic rbac roles|assign|revoke|check|import|service-account|invite|offboard` over the routes above; a server refusal exits 2 with its message, a usage error 64, an invalid CSV 3. `agentic login --invite <link>` redeems an invite and stores both secrets in the OS keyring.

**Tech Stack:** Python 3.12; control plane as pinned (FastAPI, SQLAlchemy Core, asyncpg, alembic, pycasbin through `agentic_cp.rbac.authz`); httpx2 (already a core dependency) for the TokenLean client; stdlib `smtplib`, `email`, `csv`, `string.Template`, `secrets`. New dev-only dependency: `aiosmtpd==1.4.6` (Apache-2.0, pure Python, 2024-05-18) for the capture SMTP server in tests.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` §12.3 (service account: `run.trigger` only), §13 (identity), §23 (onboarding step 1, invite email; admin tasks table: issue/revoke/rotate keys, budget alerts), §25 (SMTP relay), open question 1 (org relay). SP15 parent plan "Slice 2" rows M2-07, M2-14, M2-16, M2-17, M2-18, M2-25 and ND-13, ND-16, ND-17. TokenLean `feat/admin-keys-and-routing` `docs/config-reference.md` "/admin/keys".

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| SMTP client | stdlib `smtplib` (STARTTLS, implicit TLS, AUTH) on a worker thread | aiosmtplib (MIT, named in the parent plan) is fine but adds a runtime dependency for one send per row in a background job; `smtplib` is clearly enough. No third-party mail service (owner rule). |
| Capture SMTP server (tests) | aiosmtpd 1.4.6 (Apache-2.0; deps atpublic Apache-2.0, attrs MIT), dev group only | a hand-written server would need its own STARTTLS and protocol tests |
| TLS certificate for the STARTTLS test | trustme (MIT/Apache-2.0), already a dev dependency | committing a key would trip gitleaks |
| Message building, multipart/alternative, Message-ID | stdlib `email.message.EmailMessage`, `email.utils` | — |
| Templates | stdlib `string.Template` + `html.escape` | Jinja2 (BSD) is more than three short templates need and its autoescape must be configured right; `Template` cannot run code |
| Retry with backoff | the outbox row itself (`attempts`, `next_attempt_at`) | tenacity (Apache-2.0) retries in-process: a restart would forget the schedule; durability needs the row |
| CSV | stdlib `csv` (quoted fields, embedded newlines, `line_num`) | pandas is far too heavy |
| TokenLean client | httpx2 (core dependency, the CLI's client too) | — |
| One-time tokens | stdlib `secrets` + sha256 (the control-plane token pattern) | itsdangerous (BSD) signs, but a stored hash is what makes single use and revocation possible |

Zyroflo (via `sdlc_commons`) has no mail, CSV or key concepts.

## Rulings (recorded for the owner)

- P1 SMTP settings are deployment-wide (`AGENTIC_CP_SMTP_HOST`, `_PORT` 587, `_SECURITY` `starttls` (default) / `tls` / `none`, `_USERNAME`, `_PASSWORD`, `_FROM`, `_FROM_NAME`, `_TIMEOUT_SECONDS` 30); a tenant may have its own sender (`tenants.mail_from`, `mail_from_name`, set by the operator command `admin tenant set-mail-from`). A relay that does not offer STARTTLS is never used in clear: the mail stays queued and retries (and fails after the last attempt). Without `AGENTIC_CP_SMTP_HOST` mails stay queued (visible in `GET /v1/notifications`) and nothing is sent.
- P2 Delivery is at least once with one Message-ID per row: the row is created with the event (never lost), its dedupe key is unique (never queued twice), and it is sent while its row lock is held (two workers never send one row). Only a crash after the relay accepted and before the commit resends, with the same Message-ID. Backoff 1, 2, 4 ... minutes capped at 60, 8 attempts; a 5xx reply or a refused recipient is permanent at once.
- P3 Bodies are cleared once a mail is sent or failed (an invite body holds the one-time link); the subject, recipient and kind remain.
- P4 Recipients are only users of the tenant (forced RLS), never service accounts or disabled users. Gate waiting: the run's starter and the active people assigned the gate's approver role in the run's team (at most 20). Run budget: the starter. Team budget: the active people holding, at team scope, a role that grants `budget.raise.team` in that team.
- P5 Budget thresholds are 80 % and 100 %; run spend is the sum of ingested `step_finished` costs; team spend is the sum over the team's runs opened in the current UTC calendar month (TokenLean still enforces the team cap; its usage view can replace this measure later, M2-37). A run registered without a budget gets no run alerts; a team without `monthly_budget_usd` no team alerts.
- P6 A service account's identity is `<name>@<team>.service.invalid` (RFC 2606 `.invalid`: never deliverable), it holds only `service-account` at team scope, needs `days` (1..365) and is created by an org admin (`rbac.assign`) or a team lead of that team (`rbac.assign.team`).
- P7 CSV import is additive only (it never removes an assignment), all or nothing when applied (any error row refuses the whole file), at most 5000 rows, header `email,role,team,handle` (team and handle optional), UTF-8 with or without BOM, LF or CRLF. Each row is authorised like a single assignment; a handle needs org `rbac.assign` (ND-6).
- P8 Invites expire after 72 hours by default (`AGENTIC_CP_INVITE_HOURS`, 1..336). The link is `<AGENTIC_CP_PUBLIC_URL>/invite#<token>`: a fragment never reaches a server or its logs, and a mail scanner that opens links cannot consume it. Only `POST /v1/invites/redeem` with the token in the body consumes it, once. Without `AGENTIC_CP_PUBLIC_URL` no invite mail is queued (the assignment still succeeds and says so).
- P9 A redeemed invite binds the proxy key to the tenant's `gateway_tenant`, the user's email and the team of their first team assignment (none for org-only roles). Without a gateway admin (`AGENTIC_CP_GATEWAY_ADMIN_URL`/`_KEY`) or a `gateway_tenant` the redemption gives the control-plane token only and says how to get the key.
- P10 Offboarding needs org `rbac.assign` (a team lead cannot offboard). It is not undone through the API; a returning person is re-invited after an operator re-enables them (needs decision ND-2b-3).
- P11 A gateway 401 during a run pauses it (`gateway-key`), never fails it: after `agentic login` with a new key, `agentic resume` continues.
- P12 Proxy-key rotation through the control plane is self-service (`POST /v1/proxy-keys/rotate`, the caller's own active keys); admins revoke (`DELETE /v1/users/{email}/proxy-keys`) and re-invite.

## Global Constraints

- core never imports `sdlc_commons`, `agentic_cli` or Zyroflo; the CLI may import core and commons; server libraries stay in the control plane (import-linter). No vendor names in code, flags or messages ("gateway", not a product).
- Every new tenant-owned table (`notifications`, `invites`, `proxy_keys`) is in `TENANT_TABLES`, created with `enable_tenant_rls`, has an isolation row factory, and every new object route an `EndpointCase` (completeness guards stay green). Another tenant's user, invite or key answers exactly like a missing one (404). `invites.token_sha256` is the only new cross-tenant unique key (`GLOBAL_UNIQUE`), looked up through the registered definer function.
- Every change appends to the tenant's audit chain in the same transaction: `notification.queued|sent|failed`, `service_account.created`, `rbac.imported` (plus each `role.assigned`), `invite.created|redeemed|revoked`, `user.offboarded`, `proxy_key.issued|rotated|revoked|revoke_pending`, `budget.threshold_crossed`, `tenant.suspended|resumed`. No secret (token, key, invite link, mail body) ever enters the audit chain or a log line.
- CLI: Typer modules under `agentic_cli/commands/` with `register(app)` and one line in `COMMANDS`; `CliError(message, ExitCode.X, hint=...)`; results through `ctx.obj`; refusal 2, invalid 3, usage 64.
- Windows and macOS laptops, Linux CI: CSV read as bytes and decoded `utf-8-sig`; no POSIX-only calls; the capture SMTP server binds 127.0.0.1 on a free port.
- Fake secrets in tests are built from pieces; never `# fmt: skip` / `# fmt: off`; line length 100; pytest exit codes checked directly. TRACKER.md, DEFERRED.md and DECISIONS files are the coordinator's.

## Review Focus

1. **A mail is never lost, never queued twice and never sent twice by two workers; STARTTLS is never skipped.** Tests: Task 2 `test_enqueue_is_idempotent_per_dedupe_key`, `test_rolled_back_event_queues_nothing`; Task 3 `test_two_workers_send_a_row_once`, `test_transient_failure_backs_off_then_sends`, `test_permanent_failure_fails_at_once`, `test_last_attempt_fails`, `test_relay_without_starttls_is_never_used_in_clear`, `test_sent_mail_has_text_and_html_and_no_remote_content`.
2. **A service account can trigger runs and nothing else.** It cannot approve gates, change roles or assignments, set switches, import, or be given another role; the service-account role cannot be widened; expiry is required and at most 365 days; its token works only in its tenant. Tests: Task 4.
3. **The CSV import is idempotent and safe.** Dry run writes nothing and shows the diff; a second apply changes nothing; one bad row refuses the file with its line number (BOM, CRLF, quoted newline); a team lead's rows outside their team are errors. Tests: Task 5.
4. **An invite is single use, expiring and tenant-bound; offboarding really ends access.** Redeem twice: the second is refused; an expired or revoked invite is refused; the redeemed token works only in the invite's tenant; after offboarding the user's control-plane token is 401, their proxy key is revoked at the gateway, a `user:` kill switch matches their runs, and run registration refuses them. Tests: Task 7, Task 8.
5. **One alert per threshold crossing.** 80 % then 100 % each alert once; replaying the same ingest batch alerts nothing; a batch crossing both sends both; team spend counts this month only; tenant b's spend never alerts a. Tests: Task 9.
6. **A revoked gateway key is actionable.** The model client turns a gateway 401 into `GatewayKeyRejected`, the run pauses `gateway-key`, and the CLI's next step names `agentic login`; `agentic login` refuses a key the gateway rejects. Tests: Task 6.

---

### Task 1: Wire models, settings and the migration skeleton

**Files:**
- Modify: `core/src/agentic/orgapi/models.py`, `core/src/agentic/orgapi/__init__.py`
- Modify: `control-plane/src/agentic_cp/settings.py`, `control-plane/src/agentic_cp/tables.py`, `control-plane/src/agentic_cp/db_roles.py`
- Create: `control-plane/src/agentic_cp/migrations/versions/0010_provisioning.py`
- Modify: `control-plane/tests/isolation.py`
- Test: existing guards (`test_migrations_match_metadata`, `test_every_tenant_table_has_an_isolation_factory`, `test_tenant_isolation`, `test_definer_functions_are_registered_and_pinned`), `control-plane/tests/test_cp_settings.py`

**Interfaces:**
- Models: `ServiceAccountRequest(name, team, days)`, `ServiceAccountIssued(id, email, team, token, token_id, expires_at)`, `ServiceAccountInfo(id, email, team, status, created_at)`, `RoleImportRequest(csv, dry_run=True)`, `ImportRow(line, email, role, team, handle, action, error)`, `RoleImportResult(applied, added, unchanged, errors, rows)`, `InviteInfo(id, email, expires_at, mailed)`, `InviteRedeem(token)`, `InviteRedeemed(tenant, email, control_plane_url, control_plane_token, token_expires_at, gateway_url, gateway_key, gateway_key_id, notice)`, `OffboardRequest(reason)`, `OffboardResult(email, tokens_revoked, invites_revoked, proxy_keys_revoked, proxy_keys_pending, kill_switch)`, `ProxyKeyIssued(key, key_id, gateway_url)`, `ProxyKeysRevoked(email, revoked, pending)`, `NotificationInfo(id, kind, recipient, subject, status, attempts, next_attempt_at, last_error, created_at, sent_at)`; `Assignment.invited: bool = False`; `RunRegistration.budget_usd: str | None`.
- Settings: `public_url`, `invite_hours=72`, `smtp_host`, `smtp_port=587`, `smtp_security="starttls"`, `smtp_username`, `smtp_password`, `smtp_from`, `smtp_from_name="agentic"`, `smtp_timeout_seconds=30`, `notify_max_attempts=8`, `gateway_admin_url`, `gateway_admin_key`, `gateway_url`.
- Tables: `notifications`, `invites`, `proxy_keys`; `runs.budget_usd`, `runs.spent_usd`; `tenants.mail_from`, `tenants.mail_from_name`; function `resolve_invite(bytea)` with the `credential_lookup` policy on `invites`.

- [ ] Add the three isolation factories first; see the completeness guard fail; add tables, migration, `DEFINER_FUNCTIONS["resolve_invite"]`, `LOOKUP_POLICIES["invites"]`, `GLOBAL_UNIQUE`; guards pass.
- [ ] Settings tests first (SMTP security values, invite hours bounds, secrets never in repr); implement; commit.

### Task 2: Outbox and templates (M2-07)

**Files:**
- Create: `control-plane/src/agentic_cp/notify/__init__.py`, `notify/outbox.py`, `notify/templates.py`, `notify/templates/{invite,gate_waiting,budget}.{txt,html}`
- Test: `control-plane/tests/test_notify_outbox.py`

**Interfaces:**
- `Rendered(subject, text, html)`; `render(kind, values, *, tenant_name) -> Rendered` (`KINDS = ("invite", "gate.waiting", "budget.threshold")`; values HTML-escaped in the HTML part).
- `async enqueue(conn, *, kind, recipient, rendered, dedupe_key, actor, clock) -> UUID | None` (None when the key was already queued; audits `notification.queued` only when a row was added).

- [ ] Tests first: render each kind (text and HTML, values escaped, no `<img`, no `http` in the HTML but the link the mail is about); enqueue twice with one key gives one row and one audit record; a rolled-back transaction leaves no row; a's row is invisible in b.
- [ ] Implement; pass; commit.

### Task 3: SMTP delivery worker (M2-07)

**Files:**
- Create: `control-plane/src/agentic_cp/notify/smtp.py`, `notify/delivery.py`, `notify/api.py`
- Modify: `control-plane/src/agentic_cp/policy_sync/worker.py` (job), `control-plane/src/agentic_cp/app.py`, `control-plane/src/agentic_cp/__main__.py` (`admin tenant set-mail-from`), root `pyproject.toml` (dev `aiosmtpd`)
- Test: `control-plane/tests/test_notify_delivery.py`, `control-plane/tests/smtp_capture.py`

**Interfaces:**
- `class Notifier(Protocol): async def send(self, message: Outgoing) -> None` raising `TransientFailure` / `PermanentFailure`; `Outgoing(id, sender, sender_name, recipient, subject, text, html)`.
- `SmtpNotifier(settings)`; `notifier_for(settings) -> Notifier | None`.
- `async deliver_due(engine, tenant_id, notifier, clock, *, max_attempts=8, batch=50) -> DeliveryReport(sent, retried, failed)`; `backoff(attempts) -> timedelta`.
- `GET /v1/notifications?status=` (org `audit.read.org`).

- [ ] Tests first (aiosmtpd capture, trustme for TLS): a queued mail arrives with the tenant's sender, multipart text + HTML, Message-ID from the row id; bodies cleared after send; STARTTLS used by default; a relay without STARTTLS is never used in clear (stays pending); 4xx then success (backoff schedule); 5xx fails at once; last attempt fails; two concurrent `deliver_due` send a row once; one tenant's worker never sends b's mail; audit `.sent` / `.failed`.
- [ ] Implement; pass; commit.

### Task 4: Service accounts and the service-principal guard (M2-14)

**Files:**
- Create: `control-plane/src/agentic_cp/auth/service_accounts.py`
- Modify: `control-plane/src/agentic_cp/rbac/authz.py`, `rbac/api.py`, `rbac/defaults.py` (`SERVICE_ACCOUNT`, `SERVICE_PERMISSIONS`), `app.py`, `__main__.py` (`admin tenant suspend|resume`)
- Test: `control-plane/tests/test_service_accounts.py`, `control-plane/tests/test_cp_commands.py`

**Interfaces:**
- `POST /v1/service-accounts` -> 201 `ServiceAccountIssued`; `GET /v1/service-accounts?team=` -> `list[ServiceAccountInfo]`.
- `authorize`: a principal of kind `service` is allowed only `SERVICE_PERMISSIONS`.

- [ ] Tests first: admin and own-team lead create; developer and other-team lead refused; days missing / 0 / 366 refused; token shown once (absent from the list); the account triggers a run (run registration rbac passes) but `gate.approve` check is false, role/assignment changes, kill switches and imports are 403; assigning it another role is 422; widening the service-account role is 409; its token is unknown in tenant b; audit `service_account.created`; suspended tenant's tokens 401 and resume restores them.
- [ ] Implement; pass; commit.

### Task 5: Bulk role import from CSV (M2-14)

**Files:**
- Create: `control-plane/src/agentic_cp/rbac/importer.py`
- Modify: `control-plane/src/agentic_cp/rbac/api.py` (`POST /v1/rbac/import`)
- Test: `control-plane/tests/test_rbac_import.py`

**Interfaces:**
- `parse(text) -> tuple[list[CsvRow], list[ImportRow]]` (errors with physical line numbers); `async plan_import(conn, ctx, rows, cache) -> list[ImportRow]`; applied rows reuse the assignment writer and the invite trigger (Task 7).
- `POST /v1/rbac/import` -> 200 `RoleImportResult` (dry run: always, with error rows; apply: 422 `rbac.import_invalid` with `problems` naming each line when any row errs).

- [ ] Tests first: dry run shows add/unchanged and writes nothing; apply then re-apply is idempotent (second run all unchanged, no audit record); BOM + CRLF + quoted field with a newline keep line numbers; unknown role / bad email / unknown team / duplicate row / missing header are errors with lines; one bad row refuses the apply; a team lead's row for another team is an error; handle needs org rights; audit `rbac.imported` plus `role.assigned` per row; a's import never touches b.
- [ ] Implement; pass; commit.

### Task 6: Gateway key port, rotation, revocation and the CLI's 401 path (M2-17)

**Files:**
- Create: `control-plane/src/agentic_cp/keys/__init__.py`, `keys/gateway.py`, `keys/store.py`, `keys/api.py`
- Modify: `control-plane/src/agentic_cp/app.py` (`gateway_keys=` hook), `core/src/agentic/transport/client.py` (`GatewayKeyRejected`, `verify_gateway_key`), `core/src/agentic/transport/__init__.py`, `core/src/agentic/agents/runner.py`, `core/src/agentic/engine/engine.py` (pause `gateway-key`), `cli/src/agentic_cli/driving.py` (next step), `cli/src/agentic_cli/commands/login.py` (verify, `--rotate`), `cli/src/agentic_cli/control_plane.py`
- Test: `control-plane/tests/test_proxy_keys.py`, `control-plane/tests/fake_gateway.py`, `core/tests/transport/test_gateway_key_rejected.py`, `cli/tests/test_login_gateway.py`

**Interfaces:**
- `IssuedKey(key, key_id)`; `class GatewayKeys(Protocol)`: `configured`, `async create(*, tenant, team, user, label) -> IssuedKey`, `async revoke(key_id) -> None` (a 404 counts as revoked), `async rotate(key_id) -> IssuedKey`; `TokenLeanKeys(admin_url, admin_key, *, transport=None)`; `NoGatewayKeys`; `GatewayUnavailable`.
- `POST /v1/proxy-keys/rotate` -> `ProxyKeyIssued`; `DELETE /v1/users/{email}/proxy-keys` -> `ProxyKeysRevoked`; worker job `retry_revocations`.
- Core: `class GatewayKeyRejected(Exception)`; the async and sync model clients raise it on a 401 from the gateway; `verify_gateway_key(base_url, key, *, network=None, transport=None) -> Literal["accepted", "rejected", "unreachable"]`.

- [ ] Tests first: `TokenLeanKeys` against a MockTransport speaking the documented API (create body, key_id, rotate, 404 revoke as done, 5xx as unavailable, admin key never in errors); rotate gives a new key once and revokes the old at the fake gateway; admin revoke; another tenant's user 404; a gateway failure on revoke is `revoke_pending` and the worker retries; audit records hold key ids only; model client 401 raises `GatewayKeyRejected`; runner re-raises it out of an SDK wrapper; engine pauses `gateway-key`; `next_steps` names `agentic login`; `agentic login` refuses a rejected key (exit 2), stores an accepted one as verified, stores with a warning when the gateway is unreachable; `agentic login --rotate` stores the rotated key.
- [ ] Implement; pass; commit.

### Task 7: Invites and redemption (M2-18)

**Files:**
- Create: `control-plane/src/agentic_cp/provisioning/__init__.py`, `provisioning/invites.py`, `provisioning/api.py`
- Modify: `control-plane/src/agentic_cp/rbac/api.py` (invite on a new person's first assignment), `cli/src/agentic_cli/commands/login.py` (`--invite`), `cli/src/agentic_cli/control_plane.py` (`redeem_invite`)
- Test: `control-plane/tests/test_invites.py`, `cli/tests/test_login_invite.py`

**Interfaces:**
- `async create_invite(conn, user, *, settings, clock, actor, tenant_name) -> InviteInfo` (revokes earlier unused invites, queues the mail with dedupe key `invite:<id>`); `invite_url(public_url, token)`; `parse_invite(text) -> (base_url, token)` in core `agentic.orgapi`.
- `POST /v1/users/{email}/invite` -> 201 `InviteInfo`; `POST /v1/invites/redeem` -> 200 `InviteRedeemed` (no bearer).

- [ ] Tests first: a new person's assignment queues one invite mail whose link carries a token in the fragment; an existing person's next assignment queues none; a service account never; redeem returns a working token (`/v1/me` in the invite's tenant only) and a gateway key bound to tenant/team/user at the fake gateway; second redeem refused; expired refused; re-invite revokes the first; malformed and unknown tokens answer alike; gateway down rolls the redemption back (invite still usable) and an orphan key is revoked; audit `invite.created`/`.redeemed`, never the token; CLI `login --invite` stores both secrets, records the login, refuses a non-https link, and a refused redemption exits 2.
- [ ] Implement; pass; commit.

### Task 8: Offboarding (M2-18)

**Files:**
- Modify: `control-plane/src/agentic_cp/provisioning/api.py`
- Test: `control-plane/tests/test_offboard.py`

**Interfaces:**
- `POST /v1/users/{email}/offboard {reason}` -> `OffboardResult` (org `rbac.assign`).

- [ ] Tests first: after offboarding the user's token is 401, their unused invite cannot be redeemed, their key is revoked at the fake gateway, a `user:<email>` kill switch is active, and their run registration is refused; a team lead is refused; the last org admin cannot be offboarded; another tenant's user is 404; gateway down leaves the key pending and the worker revokes it later; audit `user.offboarded`.
- [ ] Implement; pass; commit.

### Task 9: Budget alerts and gate-waiting mail (M2-16, M2-07)

**Files:**
- Create: `control-plane/src/agentic_cp/alerts/__init__.py`, `alerts/budget.py`, `alerts/sinks.py`
- Modify: `control-plane/src/agentic_cp/ingest/store.py`, `ingest/register.py` (store `budget_usd`), `policy_sync/service.py` (`team_budgets`), `commons/src/sdlc_commons/gates.py` (`waiting(..., role=)`), `core/src/agentic/engine/engine.py` (pass the approver role), `cli/src/agentic_cli/org_preflight.py` (send `budget_usd`)
- Test: `control-plane/tests/test_budget_alerts.py`, `control-plane/tests/test_gate_mail.py`

**Interfaces:**
- `BudgetAlert(scope, subject, threshold, spent_usd, budget_usd, period, team, run_id)`; `class AlertSink(Protocol): async def deliver(self, conn, alert) -> None`; `MailAlertSink`, `class WebhookAlertSink(Protocol)`, `NoWebhook`; `crossed(before, after, budget) -> list[int]`; `async check_run(...)`, `async check_team(...)`.

- [ ] Tests first: run crossing 80 then 100 alerts once each; replayed batch alerts nothing; a batch crossing both alerts both; no budget, no alert; team spend sums this month's runs only (a run opened last month does not count); team leads get the team mail, the starter the run mail; every sink receives each alert; audit `budget.threshold_crossed`; b's runs never alert a; gate waiting mails the starter and the role holders of that team once per gate wait.
- [ ] Implement; pass; commit.

### Task 10: `agentic rbac` (M2-25)

**Files:**
- Create: `cli/src/agentic_cli/commands/rbac.py`
- Modify: `cli/src/agentic_cli/commands/__init__.py`, `cli/src/agentic_cli/control_plane.py`, `cli/tests/cp_fakes.py`
- Test: `cli/tests/test_rbac_commands.py`

**Interfaces:**
- `agentic rbac roles [NAME]`, `agentic rbac assign EMAIL ROLE [--team T]`, `agentic rbac revoke EMAIL ROLE [--team T]`, `agentic rbac check PERMISSION [--team T] [--repo R] [--user E]` (exit 2 when not allowed), `agentic rbac import FILE [--apply]`, `agentic rbac service-account NAME --team T --days N`, `agentic rbac invite EMAIL`, `agentic rbac offboard EMAIL --reason R`.

- [ ] Tests first: each command's JSON result and request; refusal per role (developer, team lead outside their team) exits 2 with the server's message; a missing argument and a bad email exit 64; `import` reads BOM + CRLF bytes, shows the diff, exits 3 on error rows, `--apply` sends `dry_run: false`; not in org mode exits 2.
- [ ] Implement; pass; commit.

### Task 11: Deferred items pulled in

- DF-510 (`agentic rbac` group): Task 10.
- DF-408 (tenant suspend and delete): suspend/resume in Task 4; delete retargeted (evidence retention, needs decision).
- DF-182 / DF-340 (gateway-key verification at login): Task 6.
- Retargeted with reasons in the track output: DF-399 and DF-407 (per-workspace control-plane tokens need ND-17's workspace-to-tenant binding), DF-344 (duplicate of DF-357, routed to a new SP6 engine budget row).

### Task 12: OpenAPI and full check

- [ ] `uv run python -m agentic_cp openapi --write`; `uv run python scripts/check.py --all` green on Windows and Linux; fix and re-run.
