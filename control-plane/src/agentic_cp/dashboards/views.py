"""The views Grafana reads (M2-37): DDL text only; migrations and `link` execute it.

**Contract views.** `usage_source` and `gateway_audit_source` have fixed, typed columns. A
migration creates them empty (`placeholder_sql`), so every dashboard view compiles before
any gateway is linked; `db link-usage` re-points them at the gateway's tables with
`CREATE OR REPLACE VIEW` (`source_sql`, the same columns, so dependent views stay).

**Tenancy.** The views run with their owner's rights, so a Grafana reader role needs no
table rights at all. Forced RLS filters every framework table by `app.tenant_id` for the
owner too. `dash_scope` adds the binding: the session's `app.tenant_id` tenant, and only
when the session is that tenant's registered reader (`tenants.dashboard_role`) or a role
that may already read the tenant tables directly (runtime, owner), whose RLS the GUC
governs anyway. A reader that sets `app.tenant_id` to another tenant gets no rows. Every
`dash_*` view reads through `dash_scope`; gateway rows match the tenant's `gateway_tenant`
(compared as a scalar, so the gateway filters them), and none match when another tenant
has the same one.

**Measures.** Unmeasured is NULL, never 0: a sum over no gateway call, a step without a
reported cost. A call without a run tag is the `(unattributed)` line of each cost view
(the label cannot be a run id, step, agent or team: none may hold parentheses). A step's
agent is the one its ledger event records, else the agent tag of its first gateway call
(`dash_step_runs.agent_source` says which).

**Executive cost (M3-01).** `dash_cost_calls` gives every call one attribution: a call whose
run tag names a registered run is `attributed` and belongs to that run's month (UTC month
it opened), owner, team and pipeline; any other call is unattributed (`no run tag`, `run not
registered`) and belongs to its own month and authenticated team. `dash_cost_facts` is the
filterable layer (one row per run step), and `driver_sql` writes the six factors of the
master plan's cost breakdown over any slice of it: each is a ratio of measured totals of the
same slice, so their product is the slice's actual cost, which the drivers views show next
to the unattributed cost with a data-quality alert above 0.5 %."""

from __future__ import annotations

from dataclasses import dataclass

#: The label of everything a call or step cannot be attributed to.
UNATTRIBUTED = "(unattributed)"

#: Where `db link-usage` imports the gateway's tables: `<schema>_gateway`.
LINK_SUFFIX = "_gateway"


@dataclass(frozen=True)
class SourceColumn:
    """One column of a contract view: its name and type, the gateway column it reads
    (`remote`), and the expression over the gateway row `g`. An `optional` column the
    gateway lacks reads NULL (unmeasured); a missing required column refuses the link."""

    name: str
    type: str
    remote: str | None
    expression: str
    optional: bool = False
    requires: str = ""


#: `usage_source`: one row per gateway call (TokenLean `usage_events`). ST-1 adds `team`,
#: ST-2 `tags`; `status_code` and `total_duration_ms` default to 0 there, which means
#: "not recorded", so 0 reads NULL here.
USAGE_COLUMNS: tuple[SourceColumn, ...] = (
    SourceColumn("request_id", "text", "request_id", "g.request_id"),
    SourceColumn("gateway_tenant", "text", "tenant_id", "g.tenant_id"),
    SourceColumn("at", "timestamptz", "timestamp", 'g."timestamp"'),
    SourceColumn("provider", "text", "provider", "NULLIF(g.provider, '')"),
    SourceColumn("model", "text", "model", "NULLIF(g.model, '')"),
    SourceColumn("routed_model", "text", "routed_model", "NULLIF(g.routed_model, '')"),
    SourceColumn("team", "text", "team", "NULLIF(g.team, '')", requires="ST-1 (team level)"),
    SourceColumn("tags", "jsonb", "tags", "g.tags", requires="ST-2 (usage tags)"),
    SourceColumn("input_tokens", "integer", "provider_prompt_tokens", "g.provider_prompt_tokens"),
    SourceColumn("output_tokens", "integer", "response_tokens", "g.response_tokens"),
    SourceColumn(
        "cache_read_tokens", "integer", "cache_read_tokens", "g.cache_read_tokens", optional=True
    ),
    SourceColumn(
        "cache_write_tokens", "integer", "cache_write_tokens", "g.cache_write_tokens", optional=True
    ),
    SourceColumn("cost_usd", "numeric", "cost_actual_usd", "g.cost_actual_usd"),
    SourceColumn("status_code", "integer", "status_code", "NULLIF(g.status_code, 0)"),
    SourceColumn("duration_ms", "integer", "total_duration_ms", "NULLIF(g.total_duration_ms, 0)"),
)

#: `gateway_audit_source`: the gateway's own audit rows (TokenLean `audit_events`).
GATEWAY_AUDIT_COLUMNS: tuple[SourceColumn, ...] = (
    SourceColumn("request_id", "text", "request_id", "g.request_id"),
    SourceColumn("gateway_tenant", "text", "tenant_id", "g.tenant_id"),
    SourceColumn("at", "timestamptz", "timestamp", 'g."timestamp"'),
    SourceColumn("action", "text", "action", "g.action"),
    SourceColumn("details", "jsonb", "details", "g.details", optional=True),
)

#: contract view -> (its columns, the gateway table it reads)
CONTRACTS: dict[str, tuple[tuple[SourceColumn, ...], str]] = {
    "usage_source": (USAGE_COLUMNS, "usage_events"),
    "gateway_audit_source": (GATEWAY_AUDIT_COLUMNS, "audit_events"),
}
CONTRACT_VIEWS: tuple[str, ...] = tuple(CONTRACTS)


def placeholder_sql(columns: tuple[SourceColumn, ...]) -> str:
    """The contract's columns, typed, over no rows."""
    listed = ", ".join(f"CAST(NULL AS {c.type}) AS {c.name}" for c in columns)
    return f"SELECT {listed} WHERE false"


def source_sql(
    columns: tuple[SourceColumn, ...], relation: str, present: frozenset[str] | None = None
) -> str:
    """The contract's columns read from `relation` (an already checked, qualified name).
    `present` names the gateway columns that exist; an optional one that does not reads
    NULL."""
    parts = []
    for column in columns:
        missing = present is not None and column.remote not in present
        expression = "NULL" if missing and column.optional else column.expression
        parts.append(f"CAST({expression} AS {column.type}) AS {column.name}")
    return f"SELECT {', '.join(parts)} FROM {relation} g"


#: Timestamps the ledger and audit chain store as text; one that does not look like ISO
#: 8601 reads NULL rather than failing the whole query.
_ISO = "'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'"
_DECIMAL = "'^-?[0-9]+([.][0-9]+)?$'"


def _at(expression: str) -> str:
    return f"CASE WHEN {expression} ~ {_ISO} THEN CAST({expression} AS timestamptz) END"


def _number(expression: str) -> str:
    """A JSON value as numeric when it is a plain decimal, else NULL."""
    return f"CASE WHEN {expression} ~ {_DECIMAL} THEN CAST({expression} AS numeric) END"


def _ext(field: str) -> str:
    return f"NULLIF(e.entry -> 'ext' -> 'agentic' ->> '{field}', '')"


def _key(expression: str) -> str:
    return f"COALESCE({expression}, '{UNATTRIBUTED}')"


def _tag(name: str) -> str:
    return f"NULLIF(u.tags ->> '{name}', '')"


#: Step outcomes that mean a limit stopped the step (spec §8.4: every limit hit is a
#: dashboard metric).
LIMIT_OUTCOMES = ("budget", "over-budget", "killed", "paused")
#: The gateway audit actions the safety panels show (guardrails, redaction, context trust,
#: tool eligibility); key management and config changes are not safety events.
SAFETY_ACTIONS = ("guardrail.%", "redaction.%", "context_trust.%", "tool_eligibility.%")


def _scope(schema: str) -> str:
    """The session's tenant, if the session may see it. Its `gateway_tenant` only when no
    other tenant has the same one: a shared gateway tenant cannot tell whose calls are
    whose, so neither tenant sees any (fail closed)."""
    return (
        "SELECT t.id AS tenant_id, t.slug, "
        "CASE WHEN NOT EXISTS (SELECT 1 FROM tenants o "
        "WHERE o.gateway_tenant = t.gateway_tenant AND o.id <> t.id) "
        "THEN t.gateway_tenant END AS gateway_tenant FROM tenants t "
        "WHERE t.id = CAST(NULLIF(current_setting('app.tenant_id', true), '') AS uuid) "
        "AND (t.dashboard_role = current_user "
        f"OR has_table_privilege(current_user, '{schema}.runs', 'SELECT'))"
    )


#: The gateway rows of the session's tenant. The tenant is compared with a scalar subquery
#: (an InitPlan), not joined: postgres_fdw then sends `tenant_id = $1` to the gateway, which
#: returns that tenant's rows only (a join filters locally, after every tenant's rows and
#: all history crossed the wire: review F2).
_GATEWAY_TENANT = "(SELECT gateway_tenant FROM dash_scope)"
_SCOPE_TENANT = "(SELECT tenant_id FROM dash_scope)"

_CALLS = f"""
SELECT {_SCOPE_TENANT} AS tenant_id, u.request_id, u.at, u.provider, u.model, u.routed_model,
  {_key("u.team")} AS team,
  {_tag("run_id")} AS run_id,
  {_key(_tag("run_id"))} AS run_key,
  {_key(_tag("step"))} AS step_key,
  {_key(_tag("agent"))} AS agent_key,
  {_tag("user")} AS tag_user, {_tag("repo")} AS repo, {_tag("pipeline")} AS pipeline,
  {_tag("tier")} AS tier, {_tag("policy_version")} AS policy_version,
  {_tag("team")} AS tag_team,
  u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
  u.cost_usd, u.status_code, u.duration_ms,
  ({_tag("run_id")} IS NOT NULL) AS attributed
FROM usage_source u WHERE u.gateway_tenant = {_GATEWAY_TENANT}
"""

#: The agent of each run step as the gateway saw it: the agent tag (and tier) of the step's
#: first call. The engine records no agent identity on its step events yet, so this is
#: where a step's agent comes from until it does (review F1); one the ledger records wins.
_STEP_AGENTS = f"""
SELECT {_SCOPE_TENANT} AS tenant_id, run_id, step_key AS step,
  (array_agg(agent_key ORDER BY at, request_id))[1] AS agent,
  (array_agg(tier ORDER BY at, request_id))[1] AS tier
FROM dash_calls
WHERE run_id IS NOT NULL AND agent_key <> '{UNATTRIBUTED}'
GROUP BY run_id, step_key
"""

_LEDGER_AGENT = _ext("agent")

#: dash_step_agents is joined on its GROUP BY key (plus the one scoped tenant) and read only
#: for the agent columns, so the planner drops the join, and its gateway read, from views
#: that show no agent (`test_views_without_an_agent_never_read_the_gateway`).
_STEP_RUNS = f"""
SELECT e.tenant_id, e.run_id, r.team, e.step, e.seq,
  COALESCE({_LEDGER_AGENT}, sa.agent) AS agent,
  CASE WHEN {_LEDGER_AGENT} IS NOT NULL THEN {_ext("tier")} ELSE sa.tier END AS tier,
  CASE WHEN {_LEDGER_AGENT} IS NOT NULL THEN 'ledger'
    WHEN sa.agent IS NOT NULL THEN 'gateway' END AS agent_source,
  e.entry ->> 'outcome' AS outcome,
  {_number("e.entry -> 'cost' ->> 'usd'")} AS reported_usd,
  st.started_at,
  {_at("e.at")} AS finished_at,
  EXTRACT(EPOCH FROM ({_at("e.at")} - st.started_at)) AS duration_s
FROM run_events e
JOIN dash_scope s ON s.tenant_id = e.tenant_id
JOIN runs r ON r.tenant_id = e.tenant_id AND r.run_id = e.run_id
LEFT JOIN LATERAL (
  SELECT {_at("b.at")} AS started_at FROM run_events b
  WHERE b.tenant_id = e.tenant_id AND b.run_id = e.run_id AND b.step = e.step
    AND b.event = 'step_started' AND b.seq < e.seq
  ORDER BY b.seq DESC LIMIT 1
) st ON true
LEFT JOIN dash_step_agents sa ON sa.tenant_id = e.tenant_id AND sa.run_id = e.run_id
  AND sa.step = e.step
WHERE e.event = 'step_finished'
"""

_GATES = f"""
SELECT e.tenant_id, e.run_id, r.team, e.step, e.seq, e.event,
  e.entry ->> 'gate' AS gate,
  e.entry ->> 'decision' AS decision,
  NULLIF(e.entry ->> 'approver', '') AS approver,
  CASE WHEN jsonb_typeof(e.entry -> 'forced') = 'boolean'
    THEN CAST(e.entry ->> 'forced' AS boolean) END AS forced,
  {_number("e.entry ->> 'waited_ms'")} AS waited_ms,
  {_ext("approver_role")} AS approver_role,
  {_at("e.at")} AS at
FROM run_events e
JOIN dash_scope s ON s.tenant_id = e.tenant_id
JOIN runs r ON r.tenant_id = e.tenant_id AND r.run_id = e.run_id
WHERE e.event IN ('gate_waiting', 'gate_decided')
"""

_RUNS = """
SELECT r.tenant_id, r.run_id, r.team, p.email AS started_by, r.status, r.policy_version,
  r.opened_at, r.last_event_at, r.budget_usd, sr.reported_usd,
  COALESCE(sr.step_executions, 0) AS step_executions,
  COALESCE(g.gates_waiting, 0) AS gates_waiting,
  COALESCE(g.approvals, 0) AS gate_approvals,
  COALESCE(g.rejections, 0) AS gate_rejections
FROM runs r
JOIN dash_scope s ON s.tenant_id = r.tenant_id
LEFT JOIN users p ON p.tenant_id = r.tenant_id AND p.id = r.started_by
LEFT JOIN (
  SELECT tenant_id, run_id, SUM(reported_usd) AS reported_usd, COUNT(*) AS step_executions
  FROM dash_step_runs GROUP BY tenant_id, run_id
) sr ON sr.tenant_id = r.tenant_id AND sr.run_id = r.run_id
LEFT JOIN (
  SELECT tenant_id, run_id,
    COUNT(*) FILTER (WHERE event = 'gate_waiting') AS gates_waiting,
    COUNT(*) FILTER (WHERE decision = 'approve') AS approvals,
    COUNT(*) FILTER (WHERE decision = 'reject') AS rejections
  FROM dash_gates GROUP BY tenant_id, run_id
) g ON g.tenant_id = r.tenant_id AND g.run_id = r.run_id
"""

_RUN_COST = f"""
WITH calls AS (
  SELECT tenant_id, run_key, MAX(run_id) AS run_id, COUNT(*) AS calls,
    SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
    SUM(cost_usd) AS gateway_usd, MIN(at) AS first_call_at, MAX(at) AS last_call_at,
    MAX(team) FILTER (WHERE team <> '{UNATTRIBUTED}') AS team,
    MAX(repo) AS repo, MAX(pipeline) AS pipeline
  FROM dash_calls GROUP BY tenant_id, run_key
), reported AS (
  SELECT tenant_id, run_id, SUM(reported_usd) AS reported_usd
  FROM dash_step_runs GROUP BY tenant_id, run_id
)
SELECT COALESCE(c.tenant_id, r.tenant_id) AS tenant_id,
  COALESCE(r.run_id, c.run_key) AS run_key,
  COALESCE(r.run_id, c.run_id) AS run_id,
  (r.run_id IS NOT NULL) AS registered,
  COALESCE(r.team, c.team, '{UNATTRIBUTED}') AS team,
  r.started_by, r.status, r.opened_at, c.repo, c.pipeline,
  c.calls, c.input_tokens, c.output_tokens, c.gateway_usd, p.reported_usd,
  c.gateway_usd - p.reported_usd AS delta_usd,
  c.first_call_at, c.last_call_at,
  COALESCE(c.last_call_at, r.opened_at) AS last_seen_at
FROM calls c
FULL JOIN dash_runs r ON r.tenant_id = c.tenant_id AND r.run_id = c.run_key
LEFT JOIN reported p ON p.tenant_id = COALESCE(r.tenant_id, c.tenant_id)
  AND p.run_id = COALESCE(r.run_id, c.run_id)
"""

_STEP_COST = """
WITH calls AS (
  SELECT tenant_id, run_key, step_key, COUNT(*) AS calls,
    SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
    SUM(cost_usd) AS gateway_usd,
    string_agg(DISTINCT agent_key, ', ' ORDER BY agent_key) AS agents
  FROM dash_calls GROUP BY tenant_id, run_key, step_key
), reported AS (
  SELECT tenant_id, run_id, step, COUNT(*) AS executions, SUM(reported_usd) AS reported_usd,
    string_agg(DISTINCT agent, ', ' ORDER BY agent) AS agents
  FROM dash_step_runs GROUP BY tenant_id, run_id, step
)
SELECT COALESCE(c.tenant_id, r.tenant_id) AS tenant_id,
  COALESCE(c.run_key, r.run_id) AS run_key,
  COALESCE(c.step_key, r.step) AS step_key,
  COALESCE(r.agents, c.agents) AS agents,
  r.executions, c.calls, c.input_tokens, c.output_tokens, c.gateway_usd, r.reported_usd,
  c.gateway_usd - r.reported_usd AS delta_usd
FROM calls c
FULL JOIN reported r ON r.tenant_id = c.tenant_id AND r.run_id = c.run_key
  AND r.step = c.step_key
"""

_AGENT_COST = f"""
WITH calls AS (
  SELECT tenant_id, agent_key, COUNT(*) AS calls, COUNT(DISTINCT run_id) AS runs,
    SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
    SUM(cost_usd) AS gateway_usd
  FROM dash_calls GROUP BY tenant_id, agent_key
), reported AS (
  SELECT tenant_id, {_key("agent")} AS agent_key, COUNT(*) AS executions,
    SUM(reported_usd) AS reported_usd
  FROM dash_step_runs GROUP BY tenant_id, {_key("agent")}
)
SELECT COALESCE(c.tenant_id, r.tenant_id) AS tenant_id,
  COALESCE(c.agent_key, r.agent_key) AS agent_key,
  r.executions, c.calls, c.runs, c.input_tokens, c.output_tokens, c.gateway_usd,
  r.reported_usd, c.gateway_usd - r.reported_usd AS delta_usd
FROM calls c
FULL JOIN reported r ON r.tenant_id = c.tenant_id AND r.agent_key = c.agent_key
"""

_TEAM_COST = """
WITH calls AS (
  SELECT tenant_id, team, COUNT(*) AS calls, COUNT(DISTINCT run_id) AS tagged_runs,
    SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
    SUM(cost_usd) AS gateway_usd
  FROM dash_calls GROUP BY tenant_id, team
), registered AS (
  SELECT tenant_id, team, COUNT(*) AS runs, COUNT(DISTINCT started_by) AS developers,
    SUM(reported_usd) AS reported_usd
  FROM dash_runs GROUP BY tenant_id, team
)
SELECT COALESCE(c.tenant_id, r.tenant_id) AS tenant_id,
  COALESCE(c.team, r.team) AS team,
  r.runs, r.developers, c.calls, c.tagged_runs, c.input_tokens, c.output_tokens,
  c.gateway_usd, r.reported_usd, c.gateway_usd - r.reported_usd AS delta_usd
FROM calls c
FULL JOIN registered r ON r.tenant_id = c.tenant_id AND r.team = c.team
"""

_LIMITED = " OR ".join(
    [f"outcome IN ({', '.join(repr(o) for o in LIMIT_OUTCOMES)})", "outcome LIKE 'stopped-%'"]
)

_AGENT_QUALITY = f"""
WITH steps AS (
  SELECT tenant_id, {_key("agent")} AS agent, run_id, step, outcome FROM dash_step_runs
), executions AS (
  SELECT tenant_id, agent, COUNT(*) AS executions,
    COUNT(*) FILTER (WHERE outcome = 'completed') AS completed,
    COUNT(*) FILTER (WHERE outcome IN ('error', 'invalid')) AS failed,
    COUNT(*) FILTER (WHERE {_LIMITED}) AS limited,
    COUNT(DISTINCT (run_id, step)) AS step_slots
  FROM steps GROUP BY tenant_id, agent
), decisions AS (
  SELECT a.tenant_id, a.agent,
    COUNT(*) FILTER (WHERE g.decision = 'approve') AS approvals,
    COUNT(*) FILTER (WHERE g.decision = 'reject') AS rejections
  FROM dash_gates g
  JOIN (SELECT DISTINCT tenant_id, run_id, step, agent FROM steps) a
    ON a.tenant_id = g.tenant_id AND a.run_id = g.run_id AND a.step = g.step
  WHERE g.event = 'gate_decided' GROUP BY a.tenant_id, a.agent
)
SELECT x.tenant_id, x.agent, x.executions, x.completed, x.failed, x.limited,
  x.executions - x.step_slots AS repeats,
  COALESCE(d.approvals, 0) AS approvals, COALESCE(d.rejections, 0) AS rejections,
  CASE WHEN COALESCE(d.approvals, 0) + COALESCE(d.rejections, 0) > 0
    THEN ROUND(CAST(d.rejections AS numeric) / (d.approvals + d.rejections), 4) END
    AS rejection_rate,
  c.gateway_usd,
  CASE WHEN d.approvals > 0 THEN ROUND(c.gateway_usd / d.approvals, 6) END AS usd_per_approval
FROM executions x
LEFT JOIN decisions d ON d.tenant_id = x.tenant_id AND d.agent = x.agent
LEFT JOIN dash_agent_cost c ON c.tenant_id = x.tenant_id AND c.agent_key = x.agent
"""

_LIMIT_EVENTS = f"""
SELECT sr.tenant_id, sr.finished_at AS at, 'step' AS source, sr.outcome AS kind, sr.team,
  sr.run_id, sr.step, sr.agent, CAST(NULL AS text) AS scope, CAST(NULL AS text) AS reason
FROM dash_step_runs sr WHERE {_LIMITED.replace("outcome", "sr.outcome")}
UNION ALL
SELECT k.tenant_id, k.created_at, 'kill_switch', k.mode, k.team, NULL, NULL, NULL,
  k.scope_kind || ':' || k.scope_value, k.reason
FROM kill_switches k JOIN dash_scope s ON s.tenant_id = k.tenant_id
UNION ALL
SELECT k.tenant_id, k.cleared_at, 'kill_switch', k.mode || '-cleared', k.team, NULL, NULL,
  NULL, k.scope_kind || ':' || k.scope_value, k.reason
FROM kill_switches k JOIN dash_scope s ON s.tenant_id = k.tenant_id
WHERE k.cleared_at IS NOT NULL
"""

_SAFETY_MATCH = " OR ".join(f"a.action LIKE '{pattern}'" for pattern in SAFETY_ACTIONS)
_GATEWAY_SAFETY = f"""
SELECT {_SCOPE_TENANT} AS tenant_id, a.at, a.action, a.request_id, a.details
FROM gateway_audit_source a
WHERE a.gateway_tenant = {_GATEWAY_TENANT} AND ({_SAFETY_MATCH})
"""

_AUDIT = f"""
SELECT a.tenant_id, a.seq, {_at("a.at")} AS at, a.actor, a.actor_kind, a.event, a.team,
  a.target, a.reason, a.run_id, a.policy_version
FROM audit_log a JOIN dash_scope s ON s.tenant_id = a.tenant_id
"""

_REPOS = """
SELECT r.tenant_id, r.repo_key, r.team, r.created_at, r.registered_by
FROM repos r JOIN dash_scope s ON s.tenant_id = r.tenant_id
"""

# ── executive cost (M3-01) ───────────────────────────────────────────────────


def _month(expression: str) -> str:
    """The UTC calendar month of a timestamptz, as `YYYY-MM`."""
    return f"to_char({expression} AT TIME ZONE 'UTC', 'YYYY-MM')"


#: Each registered run: its month, owner, team and pipeline (the one registered, else the
#: pipeline tag of its first call), and whether any of its events reached the control plane
#: (a run without one has unmeasured turns, not zero).
_COST_RUNS = f"""
SELECT r.tenant_id, r.run_id, {_month("r.opened_at")} AS month, r.opened_at,
  COALESCE(p.email, CAST(r.started_by AS text)) AS user_email, r.team,
  COALESCE(r.pipeline, t.pipeline, '{UNATTRIBUTED}') AS pipeline,
  EXISTS (SELECT 1 FROM run_events e WHERE e.tenant_id = r.tenant_id AND e.run_id = r.run_id)
    AS has_events
FROM runs r
JOIN dash_scope s ON s.tenant_id = r.tenant_id
LEFT JOIN users p ON p.tenant_id = r.tenant_id AND p.id = r.started_by
LEFT JOIN (
  SELECT run_id, (array_agg(pipeline ORDER BY at, request_id))[1] AS pipeline
  FROM dash_calls WHERE run_id IS NOT NULL AND pipeline IS NOT NULL GROUP BY run_id
) t ON t.run_id = r.run_id
"""

#: Each step of a run, as the ledger saw it: how often it started (every loop iteration and
#: retry) and its agent (the ledger's, else the agent tag of the step's first call).
_COST_STEPS = f"""
SELECT e.tenant_id, e.run_id, e.step,
  COUNT(*) FILTER (WHERE e.event = 'step_started') AS executions,
  COALESCE(MAX({_LEDGER_AGENT}), MAX(sa.agent)) AS agent
FROM run_events e
JOIN dash_scope s ON s.tenant_id = e.tenant_id
LEFT JOIN dash_step_agents sa ON sa.tenant_id = e.tenant_id AND sa.run_id = e.run_id
  AND sa.step = e.step
WHERE e.event IN ('step_started', 'step_finished')
GROUP BY e.tenant_id, e.run_id, e.step
"""

#: A call's tokens: the parts the gateway measured (a missing cache count is not 0, it is
#: left out); NULL when it measured none.
_PARTS = ("c.input_tokens", "c.output_tokens", "c.cache_read_tokens", "c.cache_write_tokens")
_CALL_TOKENS = (
    f"CASE WHEN num_nonnulls({', '.join(_PARTS)}) > 0 "
    f"THEN {' + '.join(f'COALESCE({part}, 0)' for part in _PARTS)} END"
)

_COST_CALLS = f"""
SELECT c.tenant_id, c.request_id, c.at,
  COALESCE(r.month, {_month("c.at")}) AS month,
  (r.run_id IS NOT NULL) AS attributed,
  CASE WHEN c.run_id IS NULL THEN 'no run tag'
    WHEN r.run_id IS NULL THEN 'run not registered' END AS unattributed_reason,
  c.run_id, r.user_email,
  COALESCE(r.team, c.team) AS team,
  COALESCE(r.pipeline, c.pipeline, '{UNATTRIBUTED}') AS pipeline,
  c.step_key AS step,
  COALESCE(st.agent, sa.agent, '{UNATTRIBUTED}') AS agent,
  COALESCE(c.routed_model, c.model, '{UNATTRIBUTED}') AS model,
  c.input_tokens, c.output_tokens, c.cache_read_tokens, c.cache_write_tokens,
  {_CALL_TOKENS} AS tokens,
  c.cost_usd
FROM dash_calls c
LEFT JOIN dash_cost_runs r ON r.tenant_id = c.tenant_id AND r.run_id = c.run_id
LEFT JOIN dash_cost_steps st ON st.tenant_id = c.tenant_id AND st.run_id = c.run_id
  AND st.step = c.step_key
LEFT JOIN dash_step_agents sa ON sa.tenant_id = c.tenant_id AND sa.run_id = c.run_id
  AND sa.step = c.step_key
"""

_SUMS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "tokens")

#: One row per (registered run, step): step executions next to the step's calls. A run with
#: neither keeps one row (step NULL). Executions are NULL on every row of a run none of whose
#: events arrived, even when its calls did (review F2): its turns are unmeasured, not 0.
#: Unattributed calls follow as grouped rows with `attributed = false`.
_COST_FACTS = f"""
SELECT r.tenant_id, r.month, true AS attributed, CAST(NULL AS text) AS unattributed_reason,
  r.run_id, r.user_email, r.team, r.pipeline, x.step,
  COALESCE(x.agent, '{UNATTRIBUTED}') AS agent,
  CASE WHEN r.has_events THEN COALESCE(x.executions, 0) END AS executions,
  COALESCE(x.calls, 0) AS calls,
  {", ".join(f"x.{name}" for name in _SUMS)}, x.cost_usd
FROM dash_cost_runs r
LEFT JOIN (
  SELECT COALESCE(st.tenant_id, ca.tenant_id) AS tenant_id,
    COALESCE(st.run_id, ca.run_id) AS run_id,
    COALESCE(st.step, ca.step) AS step,
    COALESCE(st.agent, ca.agent) AS agent,
    st.executions, ca.calls, {", ".join(f"ca.{name}" for name in _SUMS)}, ca.cost_usd
  FROM dash_cost_steps st
  FULL JOIN (
    SELECT tenant_id, run_id, step, MAX(agent) AS agent, COUNT(*) AS calls,
      {", ".join(f"SUM({name}) AS {name}" for name in _SUMS)}, SUM(cost_usd) AS cost_usd
    FROM dash_cost_calls WHERE attributed GROUP BY tenant_id, run_id, step
  ) ca ON ca.tenant_id = st.tenant_id AND ca.run_id = st.run_id AND ca.step = st.step
) x ON x.tenant_id = r.tenant_id AND x.run_id = r.run_id
UNION ALL
SELECT tenant_id, month, false, unattributed_reason, run_id, NULL, team, pipeline, step,
  agent, 0, COUNT(*), {", ".join(f"SUM({name})" for name in _SUMS)}, SUM(cost_usd)
FROM dash_cost_calls WHERE NOT attributed
GROUP BY tenant_id, month, unattributed_reason, run_id, team, pipeline, step, agent
"""

#: The six factors in product order: (column, label).
FACTORS: tuple[tuple[str, str], ...] = (
    ("users", "Users"),
    ("sessions_per_user", "Sessions per user"),
    ("turns_per_session", "Turns per session"),
    ("requests_per_turn", "Requests per turn"),
    ("tokens_per_request", "Tokens per request"),
    ("cost_per_token", "Cost per token"),
)
#: Above this difference between the product of the factors and the actual cost, the
#: drivers views raise `data_quality_alert` (master plan: 0.5 %).
ALERT_PCT = "0.5"

_FACTOR_SQL = {
    "users": "t.users",
    "sessions_per_user": "CAST(t.sessions AS numeric) / NULLIF(t.users, 0)",
    "turns_per_session": "CAST(t.turns AS numeric) / NULLIF(t.sessions, 0)",
    "requests_per_turn": "CAST(t.requests AS numeric) / NULLIF(t.turns, 0)",
    "tokens_per_request": "CAST(t.tokens AS numeric) / NULLIF(t.requests, 0)",
    "cost_per_token": "t.actual_usd / NULLIF(t.tokens, 0)",
}
_PRODUCT = " * ".join(f"({_FACTOR_SQL[name]})" for name, _ in FACTORS)
_TOTAL = (
    "CASE WHEN t.actual_usd IS NULL AND t.unattributed_usd IS NULL THEN NULL "
    "ELSE COALESCE(t.actual_usd, 0) + COALESCE(t.unattributed_usd, 0) END"
)


def driver_sql(where: str = "true", group: tuple[str, ...] = ()) -> str:
    """The cost drivers of the `dash_cost_facts f` rows matching `where`, one row per `group`
    (no group: exactly one row, even over no rows). Columns: the group, the measured totals
    (users, sessions, turns, requests, tokens and their split), the six factors, the product
    of the factors, the attributed actual cost, the unattributed cost, their total, the
    difference in percent, `data_quality_alert` and `cached_share`."""
    keys = "".join(f"f.{name} AS {name}, " for name in group)
    outer = "".join(f"t.{name}, " for name in group)
    grouping = f" GROUP BY {', '.join(f'f.{name}' for name in group)}" if group else ""
    split = ", ".join(f"SUM(f.{name}) FILTER (WHERE f.attributed) AS {name}" for name in _SUMS)
    inner = (
        f"SELECT {keys}"
        "COUNT(DISTINCT f.user_email) FILTER (WHERE f.attributed) AS users, "
        "COUNT(DISTINCT f.run_id) FILTER (WHERE f.attributed) AS sessions, "
        "SUM(f.executions) FILTER (WHERE f.attributed) AS turns, "
        "SUM(f.calls) FILTER (WHERE f.attributed) AS requests, "
        f"{split}, "
        "SUM(f.cost_usd) FILTER (WHERE f.attributed) AS actual_usd, "
        "SUM(f.cost_usd) FILTER (WHERE NOT f.attributed) AS unattributed_usd, "
        "SUM(f.calls) FILTER (WHERE NOT f.attributed) AS unattributed_requests, "
        "SUM(f.cache_read_tokens) AS all_cache_read, SUM(f.tokens) AS all_tokens "
        f"FROM dash_cost_facts f WHERE {where}{grouping}"
    )
    factors = ", ".join(f"{_FACTOR_SQL[name]} AS {name}" for name, _ in FACTORS)
    return (
        f"SELECT {outer}t.sessions, t.turns, t.requests, "
        f"{', '.join(f't.{name}' for name in _SUMS)}, {factors}, "
        f"{_PRODUCT} AS product_usd, t.actual_usd, t.unattributed_usd, "
        f"t.unattributed_requests, {_TOTAL} AS total_usd, "
        f"ROUND(100 * ABS({_PRODUCT} - ({_TOTAL})) / NULLIF({_TOTAL}, 0), 4) AS diff_pct, "
        f"CASE WHEN ({_TOTAL}) IS NULL THEN false WHEN ({_PRODUCT}) IS NULL THEN true "
        f"WHEN ({_TOTAL}) = 0 THEN ({_PRODUCT}) <> 0 "
        f"ELSE 100 * ABS({_PRODUCT} - ({_TOTAL})) / ({_TOTAL}) > {ALERT_PCT} END "
        "AS data_quality_alert, "
        "ROUND(CAST(t.all_cache_read AS numeric) / NULLIF(t.all_tokens, 0), 6) AS cached_share "
        f"FROM ({inner}) t"
    )


_CHARGEBACK = f"""
SELECT f.tenant_id, f.month, f.team,
  COUNT(DISTINCT f.user_email) FILTER (WHERE f.attributed) AS users,
  COUNT(DISTINCT f.run_id) FILTER (WHERE f.attributed) AS runs,
  SUM(f.executions) AS step_executions,
  SUM(f.calls) AS calls,
  {", ".join(f"SUM(f.{name}) AS {name}" for name in _SUMS)},
  SUM(f.cost_usd) FILTER (WHERE f.attributed) AS attributed_usd,
  SUM(f.cost_usd) FILTER (WHERE NOT f.attributed) AS unattributed_usd,
  SUM(f.cost_usd) AS cost_usd
FROM dash_cost_facts f
GROUP BY f.tenant_id, f.month, f.team
"""

#: ROI measures per month and team. Measured: runs, users, step executions and repeats (a
#: step run again in the same run: review loops, retries) and their rate, gate decisions
#: and the human wait at them (a decision without a recorded wait is counted, its wait is
#: not), total cost. Not measured yet, so NULL (Zyroflo metrics doctrine): the PR measures
#: (no pull-request outcome reaches the control plane), DORA lead time and change-failure
#: rate (defined against production; no deployment is recorded) and gateway savings
#: (optimisations are off until the ST-7 opt-in).
_ROI_MONTH = """
SELECT f.tenant_id, f.month, f.team, f.runs, f.users, f.step_executions,
  f.step_executions - f.step_slots AS step_repeats,
  ROUND(CAST(f.step_executions - f.step_slots AS numeric) / NULLIF(f.step_executions, 0), 4)
    AS step_rework_rate,
  COALESCE(g.gate_decisions, 0) AS gate_decisions,
  g.gate_wait_hours, g.gate_wait_median_minutes,
  f.cost_usd,
  CAST(NULL AS bigint) AS merged_changes,
  CAST(NULL AS numeric) AS cost_per_accepted_change_usd,
  CAST(NULL AS numeric) AS request_to_merge_hours,
  CAST(NULL AS numeric) AS pr_merge_rate,
  CAST(NULL AS numeric) AS dora_lead_time_hours,
  CAST(NULL AS numeric) AS change_failure_rate,
  CAST(NULL AS numeric) AS gateway_savings_usd
FROM (
  SELECT tenant_id, month, team,
    COUNT(DISTINCT run_id) FILTER (WHERE attributed) AS runs,
    COUNT(DISTINCT user_email) FILTER (WHERE attributed) AS users,
    SUM(executions) AS step_executions,
    COUNT(*) FILTER (WHERE executions > 0) AS step_slots,
    SUM(cost_usd) AS cost_usd
  FROM dash_cost_facts GROUP BY tenant_id, month, team
) f
LEFT JOIN (
  SELECT r.tenant_id, r.month, r.team, COUNT(*) AS gate_decisions,
    ROUND(SUM(g.waited_ms) / 3600000, 4) AS gate_wait_hours,
    ROUND(CAST(percentile_cont(0.5) WITHIN GROUP (ORDER BY g.waited_ms) AS numeric) / 60000, 4)
      AS gate_wait_median_minutes
  FROM dash_gates g
  JOIN dash_cost_runs r ON r.tenant_id = g.tenant_id AND r.run_id = g.run_id
  WHERE g.event = 'gate_decided'
  GROUP BY r.tenant_id, r.month, r.team
) g ON g.tenant_id = f.tenant_id AND g.month = f.month AND g.team = f.team
"""

#: Each applied learning (M3-16): its agent's corrections and its team's runs in the 30 days
#: before and after it was applied, and the correction rate (corrections per run) of each
#: window. A window without a run is NULL (unmeasured), never 0.
_LEARNING_WINDOW = "INTERVAL '30 days'"
_LEARNING_EFFECT = f"""
WITH applied AS (
  SELECT p.tenant_id, p.id AS proposal_id, p.team, p.agent, p.scope, p.lesson, p.status,
    p.applied_at, p.reverted_at
  FROM learning_proposals p JOIN dash_scope s ON s.tenant_id = p.tenant_id
  WHERE p.applied_at IS NOT NULL
), corrections AS (
  SELECT a.tenant_id, a.proposal_id,
    COUNT(f.id) FILTER (WHERE f.given_at >= a.applied_at - {_LEARNING_WINDOW}
      AND f.given_at < a.applied_at) AS corrections_before,
    COUNT(f.id) FILTER (WHERE f.given_at >= a.applied_at
      AND f.given_at < a.applied_at + {_LEARNING_WINDOW}) AS corrections_after
  FROM applied a LEFT JOIN feedback f
    ON f.tenant_id = a.tenant_id AND f.team = a.team AND a.agent = ANY(f.agents)
  GROUP BY a.tenant_id, a.proposal_id
), team_runs AS (
  SELECT a.tenant_id, a.proposal_id,
    COUNT(r.run_id) FILTER (WHERE r.opened_at >= a.applied_at - {_LEARNING_WINDOW}
      AND r.opened_at < a.applied_at) AS runs_before,
    COUNT(r.run_id) FILTER (WHERE r.opened_at >= a.applied_at
      AND r.opened_at < a.applied_at + {_LEARNING_WINDOW}) AS runs_after
  FROM applied a LEFT JOIN runs r ON r.tenant_id = a.tenant_id AND r.team = a.team
  GROUP BY a.tenant_id, a.proposal_id
)
SELECT a.tenant_id, a.proposal_id, a.team, a.agent, a.scope, a.lesson, a.status,
  a.applied_at, a.reverted_at, c.corrections_before, c.corrections_after,
  t.runs_before, t.runs_after,
  CASE WHEN t.runs_before > 0
    THEN ROUND(CAST(c.corrections_before AS numeric) / t.runs_before, 4) END AS rate_before,
  CASE WHEN t.runs_after > 0
    THEN ROUND(CAST(c.corrections_after AS numeric) / t.runs_after, 4) END AS rate_after
FROM applied a
JOIN corrections c ON c.tenant_id = a.tenant_id AND c.proposal_id = a.proposal_id
JOIN team_runs t ON t.tenant_id = a.tenant_id AND t.proposal_id = a.proposal_id
"""

#: The views the M2-37 migration creates, in creation order (each reads only those before
#: it). Views over newer tables are created by the migration that adds those tables.
M2_37_VIEWS: tuple[str, ...] = (
    "dash_scope",
    "dash_calls",
    "dash_step_agents",
    "dash_step_runs",
    "dash_gates",
    "dash_runs",
    "dash_run_cost",
    "dash_step_cost",
    "dash_agent_cost",
    "dash_team_cost",
    "dash_agent_quality",
    "dash_limit_events",
    "dash_gateway_safety",
    "dash_audit",
    "dash_repos",
)
#: The learning loop's views (migration `m3_14_learning_apply`; they read the M3-11 tables).
LEARNING_VIEWS: tuple[str, ...] = ("dash_learning_effect",)

#: The executive cost views of M3-01 (migration `m3_01_roi`), in creation order.
ROI_VIEWS: tuple[str, ...] = (
    "dash_cost_runs",
    "dash_cost_steps",
    "dash_cost_calls",
    "dash_cost_facts",
    "dash_cost_drivers",
    "dash_cost_drivers_team",
    "dash_cost_drivers_user",
    "dash_chargeback",
    "dash_roi_month",
)

#: Every dashboard view, in creation order. A Grafana reader may select exactly these;
#: `test_every_dash_view_is_registered` keeps the list equal to the schema's `dash_*` views.
DASH_VIEWS: tuple[str, ...] = M2_37_VIEWS + LEARNING_VIEWS + ROI_VIEWS


def _bodies(schema: str) -> dict[str, str]:
    return {
        "dash_scope": _scope(schema),
        "dash_calls": _CALLS,
        "dash_step_agents": _STEP_AGENTS,
        "dash_step_runs": _STEP_RUNS,
        "dash_gates": _GATES,
        "dash_runs": _RUNS,
        "dash_run_cost": _RUN_COST,
        "dash_step_cost": _STEP_COST,
        "dash_agent_cost": _AGENT_COST,
        "dash_team_cost": _TEAM_COST,
        "dash_agent_quality": _AGENT_QUALITY,
        "dash_limit_events": _LIMIT_EVENTS,
        "dash_gateway_safety": _GATEWAY_SAFETY,
        "dash_audit": _AUDIT,
        "dash_repos": _REPOS,
        "dash_cost_runs": _COST_RUNS,
        "dash_cost_steps": _COST_STEPS,
        "dash_cost_calls": _COST_CALLS,
        "dash_cost_facts": _COST_FACTS,
        "dash_cost_drivers": driver_sql(group=("tenant_id", "month")),
        "dash_cost_drivers_team": driver_sql(group=("tenant_id", "month", "team")),
        "dash_cost_drivers_user": driver_sql("f.attributed", ("tenant_id", "month", "user_email")),
        "dash_chargeback": _CHARGEBACK,
        "dash_roi_month": _ROI_MONTH,
        "dash_learning_effect": _LEARNING_EFFECT,
    }


def view_statements(schema: str, views: tuple[str, ...] = M2_37_VIEWS) -> list[str]:
    """`CREATE VIEW` for `views`, in order; the M2-37 set also creates the contracts (empty)
    first. `schema` must already be a checked identifier (`tenancy.rls.schema_of`)."""
    statements = []
    if views == M2_37_VIEWS:
        statements += [
            f"CREATE VIEW {name} AS {placeholder_sql(columns)}"
            for name, (columns, _) in CONTRACTS.items()
        ]
    bodies = _bodies(schema)
    statements += [f"CREATE VIEW {name} AS {bodies[name].strip()}" for name in views]
    return statements


def drop_statements(views: tuple[str, ...] = M2_37_VIEWS) -> list[str]:
    """`DROP VIEW` for every view `view_statements(schema, views)` creates, dependents
    first."""
    contracts = CONTRACT_VIEWS if views == M2_37_VIEWS else ()
    return [f"DROP VIEW IF EXISTS {name}" for name in (*reversed(views), *contracts)]


def learning_view_statements(schema: str) -> list[str]:
    """`CREATE VIEW` for the learning loop's views (M3-16), after `view_statements`."""
    return view_statements(schema, LEARNING_VIEWS)
