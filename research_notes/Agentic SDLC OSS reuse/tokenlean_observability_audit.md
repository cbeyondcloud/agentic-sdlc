# TokenLean observability, audit and accounting — code review (2026-10-16)

Source: read-only review of d:\token-optimisation. "Commercial" = paths listed as commercial in .gitignore (src/proxy/api/*, commercial_app.py, src/portal/, billing/{tiers,invoicing,payments}.py, audit/attestation.py).

## Reusable today (OSS)
- **Prometheus** (middleware/g18_observability.py:174-445): usage/cost counters labelled [model, team, feature, tenant_id]; latency, failover, trust & safety by tenant. Cardinality guard `_bounded_label` + allowlist `groups.G18_observability.label_values.{team,feature}`. No user/provider label on cost; labels fixed (no arbitrary header dims). `team` only trusted from gateway keys (tenancy/resolver.py:118-167).
- **Postgres `usage_events`** (billing/models.py:86-185, metering.py): per-request row — tenant, request_id, ts, user_id, model, routed_model, provider, protocol, tokens (prompt/response/cache), cost_actual/baseline/saved, cache_hit, complexity_tier, status, durations, otel_trace_id. Missing: team, feature, workflow/run/step/agent/parent/repo, reasoning tokens. OSS export: POST /admin/usage-export (reduced columns).
- **Grafana** (dashboard/dashboards): 10 OSS dashboards (Billing, Latency, Live Calls, Per Call, Quarterly, Requests Explorer, SLA, Tenant Overview, Trends, Trust & Safety) on Prometheus + AppDB + LangfuseDB. Dimensions tenant/user/model; nothing per pipeline/step/agent.
- **Langfuse** (middleware/langfuse_tracing.py): trace per request with user_id, session_id, tokens, cost metadata; prompt/response content only if `capture_trace_content` (default off); PII-aware (skips content if PII flagged; re-masks in mask mode). One shared Langfuse project, no per-tenant ACL. BUG: streamed calls never call finish_trace (no usage in Langfuse).
- **OTel spans** (tracing/otel.py): proxy-pipeline + per-stage spans; returns X-Trace-ID (also on streams); stored in usage_events.otel_trace_id. Does NOT extract incoming traceparent; no gen_ai.* attrs.
- **Audit engine** (audit/log.py): `audit_events` table (tenant, request_id, ts, action, user_id, details JSONB); OSS writes PII-free security events only (guardrail, redaction, context_trust, tool_eligibility, completion.empty). Optional append-only DB role (audit/enforcement.py). No hash chain. Retention `retention.audit_days`.
- **Events seam** (events.py): spend_cap.reached, budget.threshold, guardrail.block, pii.detected, trial.*; new types easy (constant + schedule_event). Delivery (HMAC webhooks) commercial.
- **Response headers** (main.py:1645-1718): x-tokenlean-request-id, -routed-model, -cache, -tokens-saved, -pct-saved, -cost-saved-usd, -latency-ms (+conditional cache/substitution headers). No actual-cost header (body `_token_opt.cost_actual_usd`, OpenAI non-stream only). Real streams: NO x-tokenlean headers/trailers/final event.

## Commercial-only (present in folder, private repo)
- Portal usage APIs incl. per-call ledger and rollups (tenant/user/day/model/provider/protocol/cache/complexity), /finops/*, React portal.
- Config-change audit (`log_config_change`): groups diff; routing rules/tool policy WITHOUT diffs; no config versioning (tenant_configs updated in place). Actor is email or key (no human on key paths). Some admin actions unaudited (tenant create/suspend, user add/remove).
- Audit read APIs, signed attestation packs (SHA-256 + Ed25519), GDPR export/erasure (tenant-level only).

## Gotchas for the framework
- `X-Step-Name` enables G05 step-level cache keyed by step name + X-Step-Inputs-Hash + template version (24h TTL, default on) → replays responses if hash not unique.
- `X-Workflow-ID` drives G17 loop control and G11 learned output cap; `X-Session-ID` drives G10 memory.
- → Send framework identity under its own headers (e.g. X-Agentic-*); use TokenLean headers only for behaviour they trigger. Other X-* headers are routing-rule matchable but not stored.

## TokenLean enhancements needed
1. Team level: key→team binding, team column, team spend caps, team config overlays (files: tenancy/resolver.py, auth/api_key_manager.py, middleware RequestContext, billing/models.py, metering.py, g18, g00_rate_limit.py, pipeline/TenantConfigLoader, langfuse_tracing.py; commercial portal rollups).
2. `tags` JSONB on usage_events from an allowlisted header set (run, step, agent, parent, repo, pipeline).
3. `x-tokenlean-cost-usd` header; cost/usage on streams (final SSE event / trailer).
4. W3C traceparent extraction + gen_ai.* span attributes.
5. Langfuse: finish_trace on streams; per-team capture policy with admin force; per-team project/ACL.
6. Config audit with full before/after diffs, versioned configs, human actor; audit admin user/tenant actions.
7. Persist matched routing rule on usage_events.
8. Optional hash-chain / WORM sink for audit.
9. Per-user erasure; per-team prompt retention; purge JSONL exports.
