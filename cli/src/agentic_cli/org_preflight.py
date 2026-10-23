"""The org checks of preflight (M2-21; spec §8.2, §12.7, §12.8, D33): what only the control
plane can answer, asked in one call (`POST /v1/runs`) that also registers the run - its
concurrency slot - when every check passes.

The control plane checks RBAC (`run.trigger`), the kill switch (for the run and each step's
agent, model and backend), the CLI version (an old CLI is refused with the upgrade message:
exit 2), the policy version, pipeline integrity (the pipeline's bytes against the approved
file of that policy version) and the concurrency caps; it reports the team's remaining
budget as None while that is not measured (never 0). Each failed check becomes a preflight
problem `org-<check>`, so a run is refused before any spend with the exact reason.

In org mode with a control plane, a run does not start when the checks cannot run (not
signed in, or unreachable): the kill-switch state could not be confirmed (ruling R6). Org
mode without a control plane runs with a warning that the checks were skipped."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

from agentic.engine.preflight import PreflightReport, Problem
from agentic.orgapi import RunPreflight
from agentic_cli import control_plane

SKIPPED = (
    "org checks skipped: no control plane is configured (RBAC, kill switch, CLI version, "
    "concurrency and pipeline integrity are not checked)"
)


def org_checks(
    rt,
    name: str,
    report: PreflightReport,
    run_id: str,
    *,
    dry_run: bool,
    budget_usd: Decimal | None = None,
):
    """Add the org checks' problems to `report`; returns the control plane's answer, or None
    when there was none. `ControlPlaneRefused` for an old CLI (426) propagates (exit 2)."""
    ws = rt.ws
    if not ws.settings.get("org"):
        return None
    try:
        client = rt.control_plane()
    except control_plane.ControlPlaneError as error:
        hint = f" ({error.hint})" if error.hint else ""
        report.problems.append(Problem(None, "org", f"org checks cannot run: {error}{hint}"))
        return None
    if client is None:
        report.problems.append(Problem(None, "org", SKIPPED, severity="warning"))
        return None
    if not ws.team:
        report.problems.append(
            Problem(None, "org", "an org run needs a team: set team in ~/.agentic/config.yaml")
        )
        return None
    body = registration(rt, name, report, run_id, dry_run=dry_run, budget_usd=budget_usd)
    try:
        with client:
            answer: RunPreflight = client.register_run(body)
    except control_plane.ControlPlaneRefused as error:
        if error.status == 426:
            raise
        report.problems.append(Problem(None, "org", f"org checks cannot run: {error}"))
        return None
    except control_plane.ControlPlaneUnavailable as error:
        report.problems.append(
            Problem(
                None,
                "org",
                f"org checks cannot run: {error} - a run cannot start without the kill-switch "
                "state",
            )
        )
        return None
    for check in answer.checks:
        if not check.passed:
            report.problems.append(Problem(None, f"org-{check.check}", check.detail, check.value))
    return answer


def registration(
    rt,
    name: str,
    report: PreflightReport,
    run_id: str,
    *,
    dry_run: bool,
    budget_usd: Decimal | None = None,
) -> dict:
    ws = rt.ws
    definitions = ws.definitions()
    path = definitions.pipeline_file(name)
    layer = definitions.pipeline_source(name)
    pipeline_file, digest = None, None
    if path is not None:
        digest = "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if layer in ("org", "team") and ws.policy_repo is not None:
            try:
                pipeline_file = Path(path).resolve().relative_to(ws.policy_repo.resolve())
            except ValueError:
                pipeline_file = None
    agents = sorted(report.resolved.values(), key=lambda a: a.definition.name)
    body = {
        "run_id": run_id,
        "team": ws.team,
        "repo": rt.repo_key(),
        "pipeline": name,
        "pipeline_file": pipeline_file.as_posix() if pipeline_file is not None else None,
        "pipeline_sha256": digest,
        "policy_version": ws.policy().version,
        "agents": [f"{a.definition.name}@{a.definition.version}" for a in agents],
        "models": sorted({a.model for a in agents}),
        "backends": sorted({a.backend for a in agents}),
        "dry_run": dry_run,
    }
    if budget_usd is not None:  # budget alerts at 80 % and 100 % (M2-16)
        body["budget_usd"] = format(budget_usd.quantize(Decimal("0.000001")).normalize(), "f")
    return body


def plan_section(answer: RunPreflight | None) -> dict | None:
    """The org checks as `run --dry-run` shows them (None: there were none)."""
    if answer is None:
        return None
    return {
        "checks": [check.model_dump(mode="json") for check in answer.checks],
        "team_budget_remaining_usd": answer.team_budget_remaining_usd,
        "min_cli_version": answer.min_cli_version,
    }
