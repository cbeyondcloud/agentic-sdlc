"""The Grafana dashboards (M2-38, spec §14.4; M3-01 executive ROI) as data, rendered to
Grafana JSON.

The committed files in `grafana/` and `grafana-executive/` equal `rendered_files()` of each
(`python -m agentic_cp dashboards --check`, and a test). `grafana-executive/` holds the
dashboards that show each person's cost; it is provisioned into its own Grafana folder so
folder permissions can limit it to the org viewers (review F3).

Every panel reads the `dash_*` views through one datasource uid, `agentic-postgres`: each
tenant's Grafana organisation provisions that uid with the tenant's reader role (`db
dashboard-role`), so every tenant gets the same JSON and the database keeps the tenants
apart (ruling D1).

Panel SQL may use Grafana's `$__timeFilter(col)`, `$__timeGroupAlias(col, '1d')`,
`$__timeFrom()`, `$__timeTo()` and the dashboard's own variables (`IN ($team)` for a
multi-value one, `${run:sqlstring}` for a single value or a textbox). Gateway measures
without a call are
NULL: totals show "No data", never 0. Titles never name a vendor: the gateway is "gateway"."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agentic_cp.dashboards.views import FACTORS, driver_sql

GRAFANA_DIR = Path(__file__).resolve().parent / "grafana"
#: Dashboards that show each person's cost (review F3), provisioned into their own Grafana
#: folder ("Agentic Executive"). A sibling of `GRAFANA_DIR`, never inside it: a Grafana file
#: provider reads its path recursively, so a subfolder would land in the shared folder too.
GRAFANA_EXECUTIVE_DIR = GRAFANA_DIR.parent / "grafana-executive"
GRAFANA_DIRS = (GRAFANA_DIR, GRAFANA_EXECUTIVE_DIR)
DATASOURCE = {"type": "grafana-postgresql-datasource", "uid": "agentic-postgres"}
MACROS = ("timeFilter", "timeGroupAlias", "timeFrom", "timeTo")
UNATTRIBUTED = "(unattributed)"

Kind = Literal["stat", "timeseries", "table", "barchart"]


@dataclass(frozen=True)
class Panel:
    title: str
    kind: Kind
    sql: str
    unit: str | None = None
    description: str = ""
    w: int = 12
    h: int = 8
    decimals: int | None = None


@dataclass(frozen=True)
class Variable:
    """A query variable (`sql` gives its options), or a `textbox` with a `default`. `sort`
    is Grafana's: 1 alphabetical, 2 alphabetical descending."""

    name: str
    label: str
    sql: str
    multi: bool = True
    kind: Literal["query", "textbox"] = "query"
    default: str = ""
    sort: int = 1


@dataclass(frozen=True)
class Dashboard:
    uid: str
    title: str
    description: str
    panels: tuple[Panel, ...]
    variables: tuple[Variable, ...] = field(default_factory=tuple)
    time_from: str = "now-30d"
    per_person_cost: bool = False
    """Shows each person's cost (review F3): committed in `GRAFANA_EXECUTIVE_DIR` and
    provisioned into its own folder, which an operator limits to the org viewers."""


def stat(
    title: str,
    sql: str,
    unit: str | None = None,
    description: str = "",
    decimals: int | None = None,
) -> Panel:
    return Panel(title, "stat", sql, unit, description, w=6, h=4, decimals=decimals)


USD = "currencyUSD"
#: Where a step's agent comes from (`dash_step_runs.agent_source`, review F1).
AGENT_SOURCE = (
    "A step's agent is the one its run ledger records, else the agent tag of its first "
    "gateway call (agent_source: ledger or gateway); a step with neither is "
    f"{UNATTRIBUTED}."
)
TEAM = Variable("team", "Team", "SELECT DISTINCT team FROM dash_team_cost ORDER BY 1")
RUN = Variable(
    "run",
    "Run",
    "SELECT run_key FROM dash_run_cost ORDER BY last_seen_at DESC NULLS LAST LIMIT 500",
    multi=False,
)
EVENT = Variable("event", "Event", "SELECT DISTINCT event FROM dash_audit ORDER BY 1")
ACTOR = Variable("actor", "Actor", "SELECT DISTINCT actor FROM dash_audit ORDER BY 1")

ORG = Dashboard(
    uid="agentic-org",
    title="Agentic - Org overview",
    description="Cost, runs, active developers and repos across the org; cost per team.",
    panels=(
        stat(
            "Gateway cost",
            "SELECT SUM(cost_usd) AS cost FROM dash_calls WHERE $__timeFilter(at)",
            USD,
            "What the gateway charged for every call in the period, attributed or not.",
        ),
        stat(
            "Runs started", "SELECT COUNT(*) AS runs FROM dash_runs WHERE $__timeFilter(opened_at)"
        ),
        stat(
            "Active developers",
            "SELECT COUNT(DISTINCT started_by) AS developers FROM dash_runs "
            "WHERE $__timeFilter(opened_at)",
        ),
        stat(
            "Active repos",
            "SELECT COUNT(DISTINCT repo) AS repos FROM dash_calls WHERE $__timeFilter(at)",
        ),
        Panel(
            "Gateway cost per day by team",
            "timeseries",
            "SELECT $__timeGroupAlias(at, '1d'), team AS metric, SUM(cost_usd) AS value "
            "FROM dash_calls WHERE $__timeFilter(at) GROUP BY 1, 2 ORDER BY 1",
            USD,
            w=24,
        ),
        Panel(
            "Cost by team (all time)",
            "table",
            "SELECT team, runs, developers, calls, gateway_usd, reported_usd, delta_usd "
            "FROM dash_team_cost ORDER BY gateway_usd DESC NULLS LAST",
            USD,
            "Gateway cost by the authenticated team, next to the cost the engine reported. "
            f"{UNATTRIBUTED}: calls with no team.",
        ),
        Panel(
            "Run budgets (open runs)",
            "table",
            "SELECT run_id, team, started_by, budget_usd, reported_usd, "
            "ROUND(100 * reported_usd / NULLIF(budget_usd, 0), 1) AS burn_pct "
            "FROM dash_runs WHERE status = 'open' AND $__timeFilter(opened_at) "
            "ORDER BY burn_pct DESC NULLS LAST",
            description="Reported spend against each run's budget; no budget: no burn.",
        ),
    ),
)

TEAM_VIEW = Dashboard(
    uid="agentic-team",
    title="Agentic - Team view",
    description="One or more teams: runs, cost by pipeline, agent and user, limit hits.",
    variables=(TEAM,),
    panels=(
        stat(
            "Gateway cost",
            "SELECT SUM(cost_usd) AS cost FROM dash_calls "
            "WHERE $__timeFilter(at) AND team IN ($team)",
            USD,
        ),
        stat(
            "Runs",
            "SELECT COUNT(*) AS runs FROM dash_runs "
            "WHERE $__timeFilter(opened_at) AND team IN ($team)",
        ),
        stat(
            "Gate rejections",
            "SELECT SUM(gate_rejections) AS rejections FROM dash_runs "
            "WHERE $__timeFilter(opened_at) AND team IN ($team)",
        ),
        stat(
            "Limit hits",
            "SELECT COUNT(*) AS hits FROM dash_limit_events "
            "WHERE $__timeFilter(at) AND source = 'step' AND team IN ($team)",
        ),
        Panel(
            "Gateway cost per day by pipeline",
            "timeseries",
            f"SELECT $__timeGroupAlias(at, '1d'), COALESCE(pipeline, '{UNATTRIBUTED}') AS metric, "
            "SUM(cost_usd) AS value FROM dash_calls "
            "WHERE $__timeFilter(at) AND team IN ($team) GROUP BY 1, 2 ORDER BY 1",
            USD,
            w=24,
        ),
        Panel(
            "Cost by agent",
            "table",
            "SELECT agent_key AS agent, COUNT(*) AS calls, SUM(cost_usd) AS gateway_usd "
            "FROM dash_calls WHERE $__timeFilter(at) AND team IN ($team) "
            "GROUP BY agent_key ORDER BY gateway_usd DESC NULLS LAST",
            USD,
        ),
        Panel(
            "Cost by user",
            "table",
            f"SELECT COALESCE(tag_user, '{UNATTRIBUTED}') AS user_name, COUNT(*) AS calls, "
            "SUM(cost_usd) AS gateway_usd FROM dash_calls "
            "WHERE $__timeFilter(at) AND team IN ($team) "
            "GROUP BY 1 ORDER BY gateway_usd DESC NULLS LAST",
            USD,
        ),
        Panel(
            "Runs",
            "table",
            "SELECT run_id, started_by, status, opened_at, budget_usd, reported_usd, "
            "step_executions, gate_approvals, gate_rejections FROM dash_runs "
            "WHERE $__timeFilter(opened_at) AND team IN ($team) ORDER BY opened_at DESC",
        ),
        Panel(
            "Limit hits and switches",
            "table",
            "SELECT at, source, kind, run_id, step, agent, scope, reason FROM dash_limit_events "
            "WHERE $__timeFilter(at) AND team IN ($team) ORDER BY at DESC",
        ),
    ),
)

RUN_EXPLORER = Dashboard(
    uid="agentic-run-explorer",
    title="Agentic - Run explorer",
    description="Drill down: run, its steps and agents, gate decisions and gateway calls.",
    variables=(RUN,),
    panels=(
        Panel(
            "Runs: gateway and reported cost",
            "table",
            "SELECT run_key, registered, team, started_by, status, pipeline, calls, "
            "gateway_usd, reported_usd, delta_usd, last_call_at FROM dash_run_cost "
            "WHERE $__timeFilter(last_seen_at) ORDER BY last_seen_at DESC NULLS LAST",
            USD,
            f"{UNATTRIBUTED}: calls without a run tag. Not registered: tagged with a run the "
            "control plane does not know.",
            w=24,
        ),
        Panel(
            "Steps of the run",
            "table",
            "SELECT step_key AS step, agents, executions, calls, gateway_usd, reported_usd, "
            "delta_usd FROM dash_step_cost WHERE run_key = ${run:sqlstring} ORDER BY step_key",
            USD,
        ),
        Panel(
            "Step executions",
            "table",
            "SELECT seq, step, agent, tier, agent_source, outcome, reported_usd, started_at, "
            "finished_at, duration_s FROM dash_step_runs WHERE run_id = ${run:sqlstring} "
            "ORDER BY seq",
            description=AGENT_SOURCE,
        ),
        Panel(
            "Gate decisions",
            "table",
            "SELECT at, step, gate, event, decision, approver, approver_role, waited_ms "
            "FROM dash_gates WHERE run_id = ${run:sqlstring} ORDER BY seq",
        ),
        Panel(
            "Gateway calls",
            "table",
            "SELECT at, step_key AS step, agent_key AS agent, model, status_code, input_tokens, "
            "output_tokens, cost_usd, duration_ms, request_id FROM dash_calls "
            "WHERE run_key = ${run:sqlstring} ORDER BY at",
            description="Each call's request_id finds its trace in the gateway's own views.",
        ),
    ),
)

AGENT_QUALITY = Dashboard(
    uid="agentic-agent-quality",
    title="Agentic - Agent quality",
    description="Per agent version: outcomes, repeated steps, gate decisions, cost per approval.",
    panels=(
        Panel(
            "Agent versions: outcomes, gates and cost",
            "table",
            "SELECT agent, executions, completed, failed, limited, repeats, approvals, "
            "rejections, rejection_rate, gateway_usd, usd_per_approval "
            "FROM dash_agent_quality ORDER BY executions DESC",
            description="repeats: executions beyond one per run step (review loops, retries). "
            + AGENT_SOURCE,
            w=24,
        ),
        Panel(
            "Gate rejection rate by agent",
            "barchart",
            "SELECT agent, rejection_rate FROM dash_agent_quality "
            "WHERE rejection_rate IS NOT NULL ORDER BY rejection_rate DESC",
            "percentunit",
        ),
        Panel(
            "Step outcomes per day",
            "timeseries",
            "SELECT $__timeGroupAlias(finished_at, '1d'), outcome AS metric, COUNT(*) AS value "
            "FROM dash_step_runs WHERE $__timeFilter(finished_at) GROUP BY 1, 2 ORDER BY 1",
        ),
        Panel(
            "Human wait at gates",
            "table",
            "SELECT gate, COUNT(*) AS decisions, ROUND(AVG(waited_ms) / 1000.0, 1) AS avg_wait_s, "
            "ROUND(MAX(waited_ms) / 1000.0, 1) AS max_wait_s FROM dash_gates "
            "WHERE event = 'gate_decided' AND waited_ms IS NOT NULL AND $__timeFilter(at) "
            "GROUP BY gate ORDER BY avg_wait_s DESC",
            description="Only decisions that recorded a wait; unrecorded waits are not zeros.",
        ),
        Panel(
            "Decisions by approver",
            "table",
            "SELECT approver, COUNT(*) FILTER (WHERE decision = 'approve') AS approvals, "
            "COUNT(*) FILTER (WHERE decision = 'reject') AS rejections, "
            "COUNT(*) FILTER (WHERE forced) AS forced FROM dash_gates "
            "WHERE event = 'gate_decided' AND $__timeFilter(at) GROUP BY approver ORDER BY 2 DESC",
        ),
        Panel(
            "Correction rate per agent before and after each learning",
            "table",
            "SELECT agent, team, lesson, status, applied_at, corrections_before, runs_before, "
            "rate_before, corrections_after, runs_after, rate_after "
            "FROM dash_learning_effect ORDER BY applied_at DESC",
            description="Each applied learning (M3-16): its agent's corrections per run of its "
            "team in the 30 days before and after it was applied. A window without a run is "
            "empty, not zero.",
            w=24,
        ),
    ),
)

LIMITS_SAFETY = Dashboard(
    uid="agentic-limits-safety",
    title="Agentic - Limits and safety",
    description="Limit hits, kill and pause switches, and the gateway's guardrail events.",
    panels=(
        stat(
            "Limit hits",
            "SELECT COUNT(*) AS hits FROM dash_limit_events "
            "WHERE source = 'step' AND $__timeFilter(at)",
        ),
        stat(
            "Switch changes",
            "SELECT COUNT(*) AS changes FROM dash_limit_events "
            "WHERE source = 'kill_switch' AND $__timeFilter(at)",
        ),
        stat(
            "Gateway safety events",
            "SELECT COUNT(*) AS events FROM dash_gateway_safety WHERE $__timeFilter(at)",
        ),
        stat(
            "Rate-limited calls",
            "SELECT COUNT(*) AS calls FROM dash_calls"
            " WHERE status_code = 429 AND $__timeFilter(at)",
        ),
        Panel(
            "Limit hits and switches",
            "table",
            "SELECT at, source, kind, team, run_id, step, agent, scope, reason "
            "FROM dash_limit_events WHERE $__timeFilter(at) ORDER BY at DESC",
            w=24,
        ),
        Panel(
            "Gateway safety events per day",
            "timeseries",
            "SELECT $__timeGroupAlias(at, '1d'), action AS metric, COUNT(*) AS value "
            "FROM dash_gateway_safety WHERE $__timeFilter(at) GROUP BY 1, 2 ORDER BY 1",
        ),
        Panel(
            "Gateway safety events",
            "table",
            "SELECT at, action, request_id, CAST(details AS text) AS details "
            "FROM dash_gateway_safety WHERE $__timeFilter(at) ORDER BY at DESC LIMIT 500",
            description="Categories and counts only: the gateway never records the content.",
        ),
    ),
)

AUDIT_EXPLORER = Dashboard(
    uid="agentic-audit-explorer",
    title="Agentic - Audit explorer",
    description="Who did what and why: the tenant's audit chain, by actor, team and event.",
    variables=(EVENT, ACTOR),
    panels=(
        Panel(
            "Audit records",
            "table",
            "SELECT seq, at, actor, actor_kind, event, team, target, reason, run_id, "
            "policy_version FROM dash_audit "
            "WHERE $__timeFilter(at) AND event IN ($event) AND actor IN ($actor) "
            "ORDER BY seq DESC LIMIT 1000",
            w=24,
            h=12,
        ),
        Panel(
            "Records by event",
            "barchart",
            "SELECT event, COUNT(*) AS records FROM dash_audit "
            "WHERE $__timeFilter(at) AND event IN ($event) AND actor IN ($actor) "
            "GROUP BY event ORDER BY records DESC",
        ),
        Panel(
            "Records per day by actor kind",
            "timeseries",
            "SELECT $__timeGroupAlias(at, '1d'), actor_kind AS metric, COUNT(*) AS value "
            "FROM dash_audit WHERE $__timeFilter(at) AND event IN ($event) "
            "AND actor IN ($actor) GROUP BY 1, 2 ORDER BY 1",
        ),
    ),
)

ADOPTION = Dashboard(
    uid="agentic-adoption",
    title="Agentic - Adoption",
    description="Repos registered, developers running pipelines, pipelines used.",
    panels=(
        stat("Repos registered", "SELECT COUNT(*) AS repos FROM dash_repos"),
        stat(
            "Developers who ran a pipeline",
            "SELECT COUNT(DISTINCT started_by) AS developers FROM dash_runs "
            "WHERE $__timeFilter(opened_at)",
        ),
        stat(
            "Teams with runs",
            "SELECT COUNT(DISTINCT team) AS teams FROM dash_runs WHERE $__timeFilter(opened_at)",
        ),
        stat(
            "Pipelines used",
            "SELECT COUNT(DISTINCT pipeline) AS pipelines FROM dash_calls WHERE $__timeFilter(at)",
        ),
        Panel(
            "Runs per day by team",
            "timeseries",
            "SELECT $__timeGroupAlias(opened_at, '1d'), team AS metric, COUNT(*) AS value "
            "FROM dash_runs WHERE $__timeFilter(opened_at) GROUP BY 1, 2 ORDER BY 1",
            w=24,
        ),
        Panel(
            "Pipelines used",
            "table",
            f"SELECT COALESCE(pipeline, '{UNATTRIBUTED}') AS pipeline, "
            "COUNT(DISTINCT run_id) AS runs, COUNT(*) AS calls, SUM(cost_usd) AS gateway_usd "
            "FROM dash_calls WHERE $__timeFilter(at) GROUP BY 1 ORDER BY runs DESC",
        ),
        Panel(
            "Repos by team",
            "table",
            "SELECT team, COUNT(*) AS repos, MIN(created_at) AS first_registered "
            "FROM dash_repos GROUP BY team ORDER BY repos DESC",
        ),
    ),
)

# ── executive ROI (M3-01) ─────────────────────────────────────────────────────

_FACTS_OF = "SELECT DISTINCT {column} FROM dash_cost_facts ORDER BY 1"
MONTH = Variable(
    "month", "Month", "SELECT DISTINCT month FROM dash_cost_facts ORDER BY 1 DESC", sort=2
)
PIPELINE = Variable("pipeline", "Pipeline", _FACTS_OF.format(column="pipeline"))
AGENT = Variable("agent", "Agent", _FACTS_OF.format(column="agent"))
COST_TEAM = Variable("team", "Team", _FACTS_OF.format(column="team"))
TARGET_USERS = Variable("target_users", "What-if users", "", False, "textbox", "1000")

_SLICE = "f.team IN ($team) AND f.pipeline IN ($pipeline) AND f.agent IN ($agent)"
_FILTER = f"f.month IN ($month) AND {_SLICE}"
_DRIVERS = driver_sql(_FILTER)
_CURRENT = "(SELECT MAX(month) FROM dash_cost_facts WHERE month IN ($month))"
_PREVIOUS = f"to_char(to_date({_CURRENT}, 'YYYY-MM') - interval '1 month', 'YYYY-MM')"
_UNITS = {"cost_per_token": USD, "users": None}
_DECIMALS = {"cost_per_token": 8, "users": 0}
#: The factor contributions are LMDI-I (logarithmic mean Divisia index): each factor's share
#: of the change is L(c1, c0) x ln(f1 / f0), where L is the logarithmic mean of the two costs;
#: the six shares add up to the change exactly.
_LOG_MEAN = (
    "CASE WHEN c.product_usd = p.product_usd THEN c.product_usd "
    "ELSE (c.product_usd - p.product_usd) / ln(c.product_usd / p.product_usd) END"
)
_MOM_ROWS = ", ".join(
    [
        f"({n}, '{label}', CAST(p.{column} AS numeric), CAST(c.{column} AS numeric))"
        for n, (column, label) in enumerate(FACTORS, 1)
    ]
    + [f"({len(FACTORS) + 1}, 'Cost (product of factors)', p.product_usd, c.product_usd)"]
)
_MOM = (
    "SELECT v.factor, v.previous, v.current, "
    "ROUND(100 * (v.current - v.previous) / NULLIF(v.previous, 0), 2) AS change_pct, "
    "CASE WHEN v.current > 0 AND v.previous > 0 AND c.product_usd > 0 AND p.product_usd > 0 "
    f"THEN ROUND(({_LOG_MEAN}) * ln(v.current / v.previous), 6) END AS contribution_usd "
    f"FROM ({driver_sql(f'f.month = {_CURRENT} AND {_SLICE}')}) c "
    f"CROSS JOIN ({driver_sql(f'f.month = {_PREVIOUS} AND {_SLICE}')}) p "
    f"CROSS JOIN LATERAL (VALUES {_MOM_ROWS}) v(n, factor, previous, current) "
    "ORDER BY v.n"
)
_PER_USER = driver_sql(f"f.attributed AND {_FILTER}", ("user_email",))
_PER_USER_MONTH = driver_sql(f"f.attributed AND {_FILTER}", ("month", "user_email"))
_MEDIANS = " * ".join(
    f"percentile_cont(0.5) WITHIN GROUP (ORDER BY u.{column})" for column, _ in FACTORS[1:]
)
_WHAT_IF = (
    f"SELECT ROUND(CAST(CAST(${{target_users:sqlstring}} AS numeric) * {_MEDIANS} AS numeric), 2) "
    f"AS projected_usd FROM ({_PER_USER_MONTH}) u WHERE u.actual_usd IS NOT NULL"
)
_CALLS_FILTER = (
    "c.month IN ($month) AND c.team IN ($team) AND c.pipeline IN ($pipeline) "
    "AND c.agent IN ($agent)"
)
_UNMEASURED_PR = "no pull-request outcome reaches the control plane yet"
_UNMEASURED_DEPLOY = "defined against production; no deployment is recorded (Zyroflo metrics.dora)"
_ROI_ROWS = ", ".join(
    [
        "(1, 'Total cost', r.cost_usd, 'USD', '')",
        "(2, 'Human wait at gates', r.wait_hours, 'hours', "
        "'decisions without a recorded wait are not counted')",
        "(3, 'Gate decisions', r.decisions, 'count', '')",
        "(4, 'Step rework rate', r.rework, 'share', "
        "'step executions that repeat a step of the same run (review loops, retries)')",
        f"(5, 'Cost per accepted change', CAST(NULL AS numeric), 'USD', '{_UNMEASURED_PR}')",
        f"(6, 'Request to merged PR', CAST(NULL AS numeric), 'hours', '{_UNMEASURED_PR}')",
        f"(7, 'PR merge rate', CAST(NULL AS numeric), 'share', '{_UNMEASURED_PR}')",
        "(8, 'DORA lead time for changes', CAST(NULL AS numeric), 'hours', "
        f"'{_UNMEASURED_DEPLOY}')",
        f"(9, 'Change-failure rate', CAST(NULL AS numeric), 'share', '{_UNMEASURED_DEPLOY}')",
        "(10, 'Gateway savings', CAST(NULL AS numeric), 'USD', "
        "'gateway optimisations are off until the ST-7 opt-in')",
    ]
)
_ROI = (
    "SELECT v.metric, v.value, v.unit, v.reason FROM ("
    "SELECT SUM(cost_usd) AS cost_usd, SUM(gate_wait_hours) AS wait_hours, "
    "CAST(SUM(gate_decisions) AS numeric) AS decisions, "
    "ROUND(CAST(SUM(step_repeats) AS numeric) / NULLIF(SUM(step_executions), 0), 4) AS rework "
    "FROM dash_roi_month WHERE month IN ($month) AND team IN ($team)) r "
    f"CROSS JOIN LATERAL (VALUES {_ROI_ROWS}) v(n, metric, value, unit, reason) ORDER BY v.n"
)

EXECUTIVE_ROI = Dashboard(
    uid="agentic-executive-roi",
    title="Agentic - Executive ROI",
    description=(
        "Total cost as users x sessions per user x turns per session x requests per turn x "
        "tokens per request x cost per token, reconciled with the gateway's actual cost; "
        "ROI measures; what-if projection."
    ),
    variables=(MONTH, COST_TEAM, PIPELINE, AGENT, TARGET_USERS),
    time_from="now-90d",
    per_person_cost=True,
    panels=(
        stat(
            "Total cost",
            f"SELECT total_usd FROM ({_DRIVERS}) d",
            USD,
            "Actual gateway cost of the selection: attributed calls plus the unattributed line.",
        ),
        stat(
            "Product of factors",
            f"SELECT product_usd FROM ({_DRIVERS}) d",
            USD,
            "The six factors multiplied: equals the attributed actual cost.",
        ),
        stat(
            "Unattributed cost",
            f"SELECT unattributed_usd FROM ({_DRIVERS}) d",
            USD,
            "Calls without a run tag, or tagged with a run the control plane does not know. "
            "No data: there were none.",
        ),
        stat(
            "Reconciliation difference",
            f"SELECT diff_pct FROM ({_DRIVERS}) d",
            "percent",
            "Product of factors against total cost; above 0.5 % is a data-quality alert "
            "(usually untagged calls).",
            decimals=2,
        ),
        *(
            stat(
                label,
                f"SELECT {column} FROM ({_DRIVERS}) d",
                _UNITS.get(column, "none"),
                "A ratio of measured totals of the selection; NULL when unmeasured.",
                decimals=_DECIMALS.get(column, 2),
            )
            for column, label in FACTORS
        ),
        stat(
            "Cached-token share",
            f"SELECT cached_share FROM ({_DRIVERS}) d",
            "percentunit",
            "Cache-read tokens over all tokens; no data when the gateway records no cache counts.",
        ),
        stat(
            "What-if: projected monthly cost",
            _WHAT_IF,
            USD,
            "What-if users x the per-user median (per month) of every other factor.",
        ),
        Panel(
            "Cost drivers by month",
            "table",
            f"SELECT * FROM ({driver_sql(_SLICE, ('month',))}) d ORDER BY month DESC",
            description="The six factors per month, the product next to the actual and "
            "unattributed cost, and the data-quality alert.",
            w=24,
        ),
        Panel(
            "Month-over-month change by factor",
            "table",
            _MOM,
            description="The newest selected month against the month before it. "
            "contribution_usd: each factor's share of the cost change (LMDI); they add up to "
            "the change of the product.",
            w=24,
        ),
        Panel(
            "Cost drivers per user",
            "table",
            f"SELECT * FROM ({_PER_USER}) d ORDER BY actual_usd DESC NULLS LAST",
            description="Click a column header to sort by any factor.",
            w=24,
            h=10,
        ),
        Panel(
            "Cost per token by model",
            "table",
            "SELECT c.model, COUNT(*) AS requests, SUM(c.tokens) AS tokens, "
            "SUM(c.cache_read_tokens) AS cache_read_tokens, SUM(c.cost_usd) AS cost_usd, "
            "ROUND(SUM(c.cost_usd) / NULLIF(SUM(c.tokens), 0), 10) AS cost_per_token, "
            "ROUND(CAST(SUM(c.cache_read_tokens) AS numeric) / NULLIF(SUM(c.tokens), 0), 4) "
            f"AS cached_share FROM dash_cost_calls c WHERE {_CALLS_FILTER} "
            "GROUP BY c.model ORDER BY cost_usd DESC NULLS LAST",
            description="The model that served each call (routed, else requested).",
        ),
        Panel(
            "Monthly cost per team",
            "table",
            "SELECT month, team, users, runs, calls, attributed_usd, unattributed_usd, cost_usd "
            "FROM dash_chargeback WHERE month IN ($month) AND team IN ($team) "
            "ORDER BY month DESC, cost_usd DESC NULLS LAST",
            USD,
            "The chargeback export's numbers (`agentic report chargeback`).",
        ),
        Panel(
            "ROI measures",
            "table",
            _ROI,
            description="A value nobody can measure yet is empty, never 0; the reason says why.",
            w=24,
        ),
    ),
)

DASHBOARDS: tuple[Dashboard, ...] = (
    ORG,
    TEAM_VIEW,
    RUN_EXPLORER,
    AGENT_QUALITY,
    LIMITS_SAFETY,
    AUDIT_EXPLORER,
    ADOPTION,
    EXECUTIVE_ROI,
)


def _target(panel: Panel) -> dict:
    return {
        "refId": "A",
        "datasource": DATASOURCE,
        "editorMode": "code",
        "format": "time_series" if panel.kind == "timeseries" else "table",
        "rawQuery": True,
        "rawSql": panel.sql,
    }


def _layout(panels: tuple[Panel, ...]) -> list[dict]:
    """Left to right in rows of 24 columns; a panel that does not fit starts a new row."""
    x = y = row_h = 0
    grid = []
    for panel in panels:
        if x + panel.w > 24:
            x, y, row_h = 0, y + row_h, 0
        grid.append({"h": panel.h, "w": panel.w, "x": x, "y": y})
        x += panel.w
        row_h = max(row_h, panel.h)
    return grid


def _variable(variable: Variable) -> dict:
    if variable.kind == "textbox":
        chosen = {"text": variable.default, "value": variable.default}
        return {
            "name": variable.name,
            "label": variable.label,
            "type": "textbox",
            "query": variable.default,
            "current": chosen,
            "options": [{**chosen, "selected": True}],
            "hide": 0,
        }
    return {
        "name": variable.name,
        "label": variable.label,
        "type": "query",
        "datasource": DATASOURCE,
        "query": variable.sql,
        "definition": variable.sql,
        "refresh": 2,
        "sort": variable.sort,
        "multi": variable.multi,
        "includeAll": variable.multi,
        "current": {},
        "options": [],
        "hide": 0,
    }


def render(dashboard: Dashboard) -> dict:
    """The dashboard as Grafana JSON (schema 39; Grafana migrates it forward)."""
    panels = []
    for n, (panel, grid) in enumerate(
        zip(dashboard.panels, _layout(dashboard.panels), strict=True), 1
    ):
        defaults: dict = {"unit": panel.unit} if panel.unit else {}
        if panel.decimals is not None:
            defaults["decimals"] = panel.decimals
        panels.append(
            {
                "id": n,
                "type": panel.kind,
                "title": panel.title,
                "description": panel.description,
                "datasource": DATASOURCE,
                "gridPos": grid,
                "fieldConfig": {"defaults": defaults, "overrides": []},
                "options": {},
                "targets": [_target(panel)],
            }
        )
    return {
        "uid": dashboard.uid,
        "title": dashboard.title,
        "description": dashboard.description,
        "tags": ["agentic"],
        "editable": False,
        "graphTooltip": 1,
        "schemaVersion": 39,
        "version": 1,
        "timezone": "browser",
        "time": {"from": dashboard.time_from, "to": "now"},
        "refresh": "",
        "annotations": {"list": []},
        "links": [],
        "templating": {"list": [_variable(v) for v in dashboard.variables]},
        "panels": panels,
    }


def directory_of(dashboard: Dashboard) -> Path:
    """Where the dashboard's JSON is committed: one directory per Grafana folder."""
    return GRAFANA_EXECUTIVE_DIR if dashboard.per_person_cost else GRAFANA_DIR


def rendered_files(directory: Path = GRAFANA_DIR) -> dict[str, str]:
    """File name -> JSON text (sorted keys, two-space indent, LF, final newline) of the
    dashboards committed in `directory`."""
    return {
        f"{d.uid.removeprefix('agentic-')}.json": json.dumps(render(d), indent=2, sort_keys=True)
        + "\n"
        for d in DASHBOARDS
        if directory_of(d) == directory
    }
