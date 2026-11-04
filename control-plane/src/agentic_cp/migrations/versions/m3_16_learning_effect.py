"""The learning-effect view counts a personal learning's owner only and hides its lesson,
and every Grafana reader bound before `m3_14_learning_apply` may read it (DF-763, review
F9).

`dash_learning_effect` keeps its columns, so it is replaced in place and the readers who
hold it keep their grant; readers bound before M3-14 (whose grants did not name it) are
granted it here, as `m3_01_roi` did for its views; one bound later gets it from
`bind_reader`. The downgrade puts the M3-14 body back (kept below) and leaves the grants:
a grant on a view a reader may read anyway changes nothing.

Revision ID: m3_16_learning_effect
Revises: m3_06_retention
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

from agentic_cp.dashboards.readers import checked_role

revision = "m3_16_learning_effect"
down_revision = "m3_06_retention"
branch_labels = None
depends_on = None

VIEW = "dash_learning_effect"
#: The body this revision installs (frozen here: a later change to the view is a later
#: revision; `dashboards.views` holds the current one).
M3_16_BODY = """
WITH applied AS (
  SELECT p.tenant_id, p.id AS proposal_id, p.team, p.agent, p.scope, p.lesson, p.status,
    p.applied_at, p.reverted_at, p.authors,
    p.owner IS NOT NULL OR COALESCE(p.draft->>'scope', '') = 'personal' AS personal
  FROM learning_proposals p JOIN dash_scope s ON s.tenant_id = p.tenant_id
  WHERE p.applied_at IS NOT NULL
), corrections AS (
  SELECT a.tenant_id, a.proposal_id,
    COUNT(f.id) FILTER (WHERE f.given_at >= a.applied_at - INTERVAL '30 days'
      AND f.given_at < a.applied_at) AS corrections_before,
    COUNT(f.id) FILTER (WHERE f.given_at >= a.applied_at
      AND f.given_at < a.applied_at + INTERVAL '30 days') AS corrections_after
  FROM applied a LEFT JOIN feedback f
    ON f.tenant_id = a.tenant_id AND f.team = a.team AND a.agent = ANY(f.agents)
    AND (NOT a.personal OR f.author_id = ANY(a.authors))
  GROUP BY a.tenant_id, a.proposal_id
), team_runs AS (
  SELECT a.tenant_id, a.proposal_id,
    COUNT(r.run_id) FILTER (WHERE r.opened_at >= a.applied_at - INTERVAL '30 days'
      AND r.opened_at < a.applied_at) AS runs_before,
    COUNT(r.run_id) FILTER (WHERE r.opened_at >= a.applied_at
      AND r.opened_at < a.applied_at + INTERVAL '30 days') AS runs_after
  FROM applied a LEFT JOIN runs r ON r.tenant_id = a.tenant_id AND r.team = a.team
    AND (NOT a.personal OR r.started_by = ANY(a.authors))
  GROUP BY a.tenant_id, a.proposal_id
)
SELECT a.tenant_id, a.proposal_id, a.team, a.agent, a.scope,
  CASE WHEN a.personal THEN NULL ELSE a.lesson END AS lesson, a.status,
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
#: The M3-14 body (`m3_14_learning_apply`), for the downgrade.
M3_14_BODY = """
WITH applied AS (
  SELECT p.tenant_id, p.id AS proposal_id, p.team, p.agent, p.scope, p.lesson, p.status,
    p.applied_at, p.reverted_at
  FROM learning_proposals p JOIN dash_scope s ON s.tenant_id = p.tenant_id
  WHERE p.applied_at IS NOT NULL
), corrections AS (
  SELECT a.tenant_id, a.proposal_id,
    COUNT(f.id) FILTER (WHERE f.given_at >= a.applied_at - INTERVAL '30 days'
      AND f.given_at < a.applied_at) AS corrections_before,
    COUNT(f.id) FILTER (WHERE f.given_at >= a.applied_at
      AND f.given_at < a.applied_at + INTERVAL '30 days') AS corrections_after
  FROM applied a LEFT JOIN feedback f
    ON f.tenant_id = a.tenant_id AND f.team = a.team AND a.agent = ANY(f.agents)
  GROUP BY a.tenant_id, a.proposal_id
), team_runs AS (
  SELECT a.tenant_id, a.proposal_id,
    COUNT(r.run_id) FILTER (WHERE r.opened_at >= a.applied_at - INTERVAL '30 days'
      AND r.opened_at < a.applied_at) AS runs_before,
    COUNT(r.run_id) FILTER (WHERE r.opened_at >= a.applied_at
      AND r.opened_at < a.applied_at + INTERVAL '30 days') AS runs_after
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


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE VIEW {VIEW} AS {M3_16_BODY.strip()}")
    bound = op.get_bind().execute(
        sa.text("select dashboard_role from tenants where dashboard_role is not null")
    )
    for (role,) in bound.all():
        op.execute(f"GRANT SELECT ON {VIEW} TO {checked_role(role)}")


def downgrade() -> None:
    op.execute(f"CREATE OR REPLACE VIEW {VIEW} AS {M3_14_BODY.strip()}")
