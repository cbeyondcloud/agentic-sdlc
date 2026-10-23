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
`dash_*` view joins `dash_scope`; gateway rows match on the tenant's `gateway_tenant`.

**Measures.** Unmeasured is NULL, never 0: a sum over no gateway call, a step without a
reported cost. A call without a run tag is the `(unattributed)` line of each cost view
(the label cannot be a run id, step, agent or team: none may hold parentheses)."""

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
    return (
        "SELECT t.id AS tenant_id, t.slug, t.gateway_tenant FROM tenants t "
        "WHERE t.id = CAST(NULLIF(current_setting('app.tenant_id', true), '') AS uuid) "
        "AND (t.dashboard_role = current_user "
        f"OR has_table_privilege(current_user, '{schema}.runs', 'SELECT'))"
    )


_CALLS = f"""
SELECT s.tenant_id, u.request_id, u.at, u.provider, u.model, u.routed_model,
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
FROM usage_source u JOIN dash_scope s ON u.gateway_tenant = s.gateway_tenant
"""

_STEP_RUNS = f"""
SELECT e.tenant_id, e.run_id, r.team, e.step, e.seq,
  {_ext("agent")} AS agent, {_ext("tier")} AS tier,
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
SELECT s.tenant_id, a.at, a.action, a.request_id, a.details
FROM gateway_audit_source a JOIN dash_scope s ON a.gateway_tenant = s.gateway_tenant
WHERE {_SAFETY_MATCH}
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

#: Every dashboard view, in creation order (each reads only those before it). A Grafana
#: reader may select exactly these; `test_every_dash_view_is_registered` keeps the list
#: equal to the schema's `dash_*` views.
DASH_VIEWS: tuple[str, ...] = (
    "dash_scope",
    "dash_calls",
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


def _bodies(schema: str) -> dict[str, str]:
    return {
        "dash_scope": _scope(schema),
        "dash_calls": _CALLS,
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
    }


def view_statements(schema: str) -> list[str]:
    """`CREATE VIEW` for the contracts (empty) and every dashboard view, in order. `schema`
    must already be a checked identifier (`tenancy.rls.schema_of`)."""
    statements = [
        f"CREATE VIEW {name} AS {placeholder_sql(columns)}"
        for name, (columns, _) in CONTRACTS.items()
    ]
    bodies = _bodies(schema)
    statements += [f"CREATE VIEW {name} AS {bodies[name].strip()}" for name in DASH_VIEWS]
    return statements


def drop_statements() -> list[str]:
    """`DROP VIEW` for every view `view_statements` creates, dependents first."""
    return [f"DROP VIEW IF EXISTS {name}" for name in (*reversed(DASH_VIEWS), *CONTRACT_VIEWS)]
