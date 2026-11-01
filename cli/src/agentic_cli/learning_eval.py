"""A learning's regression case, run before and after its change on approve (M3-16; spec §24
step 6 "the eval case runs before and after"; DF-756).

`measure_change` runs the draft's regression case (M1-63 format) twice through the eval
harness (`agentic.learning.regression.measure` over `run_eval`): once with the agent as it is
(`before`), once with the learned block added to its instructions (`after`, the overlay
`agentic skills eval` uses, M2-46). Each answer is scored for how much of the lesson it
covers (deterministic, no judge model). The agent is resolved under this workspace's policy
and calls its model through the configured gateway, metered and capped by the policy's eval
budget for the agent - the same path as a run; tests replace the network
(`runtime.network_transport`), so no model is ever called there.

The measurement informs; it never decides. Approving is the owner's decision (D29), so a
change that scores lower is applied and said so, and a case that cannot be run (no gateway,
the agent not defined here, the eval extra missing, a failed call) is recorded as
`unmeasured` with the reason - it never blocks the approval. A measured case is written to the
run ledger (`eval.learning_measured`, Zyroflo's hash chain) and returned for the change
record and, in org mode, the control plane's `learning.applied` audit event.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from agentic.learning.changes import HEADING, Draft
from agentic.policy import resolve_agent
from agentic_cli import runtime
from agentic_cli.errors import CliError

if TYPE_CHECKING:
    from agentic.eval.records import RunRequest
    from agentic.learning.regression import Measurement
    from agentic_cli.runtime import Runtime

#: The identity step of the eval's model calls (the gateway attributes their spend to it).
STEP = "learning-eval"
#: Why a case could not run is kept with the change and sent to the control plane: masked
#: (`Runtime.redact`) and cut to this length.
MAX_REASON = 300


def overlay_block(made: Draft) -> str:
    """The text the `after` side adds to the agent's instructions: the learned block, as a
    run reads it from the changed skill, note or file."""
    return f"{HEADING}\n\n- {made.lesson}\n"


def overlay_sha(made: Draft) -> str:
    """Names what was measured: the same case and lesson give the same digest."""
    return "sha256:" + hashlib.sha256(overlay_block(made).encode("utf-8")).hexdigest()


def measure_change(
    rt: Runtime, made: Draft, *, skip: str | None, warn: Callable[[str], None]
) -> dict:
    """The regression case before and after the change, as recorded with the change:
    `status` measured, unmeasured (with `reason`) or skipped; `overlay` names the lesson."""
    head = {"case": str(made.eval_case.get("id") or ""), "overlay": overlay_sha(made)}
    if skip:
        return {"status": "skipped", **head, "reason": skip}
    try:
        found = _measure(rt, made)
    except CliError as error:
        return _unmeasured(rt, head, str(error), warn)
    except Exception as error:  # noqa: BLE001 - a measurement never blocks an approval
        return _unmeasured(rt, head, f"{type(error).__name__}: {error}", warn)
    if found.unscored:
        return _unmeasured(rt, head, "; ".join(found.unscored), warn)
    result = {"status": "measured", **found.as_dict(), **head, "agent": made.agent}
    try:
        result["ledger"] = _ledgered(rt, made, result)
    except CliError as error:
        result["ledger"] = None
        warn(f"the measurement could not be written to the run ledger: {error}")
    if found.regressed:
        warn(
            f"the regression case scored lower with the change (before {found.before:.2f}, "
            f"after {found.after:.2f}); the change is applied as approved - "
            f"`agentic learn revert {made.proposal_id}` undoes it"
        )
    return result


def _unmeasured(rt: Runtime, head: dict, reason: str, warn: Callable[[str], None]) -> dict:
    shown = rt.redact()(reason)[:MAX_REASON]
    warn(f"the regression case was not run before and after: {shown}")
    return {"status": "unmeasured", **head, "reason": shown}


def _measure(rt: Runtime, made: Draft) -> Measurement:
    from agentic.agents.metering import CostLog
    from agentic.agents.models import metered_model
    from agentic.eval.cases import EvalCase
    from agentic.eval.runner import AgentCaseRunner
    from agentic.handoffs import BUILTIN_CATALOG
    from agentic.learning.regression import measure
    from agentic.transport import BudgetLedger, CallIdentity
    from agentic_cli import evaluation
    from agentic_cli.evaluation import SkillVariant

    policy = rt.ws.policy()
    definition = rt.ws.definitions().agent(made.agent)
    resolved, violations = resolve_agent(definition, policy, rt.ws.context())
    if resolved is None:
        reasons = "; ".join(f"{v.field}: {v.reason}" for v in violations)
        raise CliError(f"policy refuses agent {made.agent} here: {reasons}")
    kind = resolved.produces
    if kind is None:
        raise CliError(f"agent {made.agent} produces no handoff: nothing to score")
    gateway = rt.gateway()
    rt.redact(gateway)  # the key is masked in anything said about a failure
    agent_id = f"{definition.name}@{definition.version}"
    money = BudgetLedger(Decimal(str(policy.eval.budget_for(made.agent))))

    def model_for(request: RunRequest, costs: CostLog, slice_):
        identity = CallIdentity(
            team=rt.ws.team or "standalone",
            user=rt.user,
            repo=rt.ws.repo.name,
            pipeline="learning",
            run_id=request.run_id,
            step=STEP,
            agent=agent_id,
            policy_version=policy.version,
            tier=resolved.tier,
            classification=resolved.classification,
        )
        model, _ = metered_model(
            resolved,
            identity,
            gateway,
            budget=slice_,
            price_for=policy.price_for,
            fallback_estimate_usd=evaluation.FALLBACK_ESTIMATE_USD,
            log=costs,
            _inner=runtime.network_transport(),
        )
        return model

    state = rt.ws.repo / ".agentic" / "local" / "learning" / "eval" / made.proposal_id

    def runner(agent) -> AgentCaseRunner:
        return AgentCaseRunner(
            agent,
            model_for,
            schema=BUILTIN_CATALOG.schema(kind),
            state_root=state,
            budget=money,
            step=STEP,
        )

    block = SkillVariant(f"learned-{made.agent}", overlay_sha(made), overlay_block(made))
    learned = evaluation.with_skill(resolved, block)
    case = EvalCase.model_validate(made.eval_case)
    return asyncio.run(measure(case, before=runner(resolved), after=runner(learned), kind=kind))


def _ledgered(rt: Runtime, made: Draft, result: dict) -> str:
    """The measurement on the run ledger (Zyroflo's hash chain): its digest, and whether
    the change regressed the case. Returns the ledger's run id."""
    from agentic_cli.evaluation import EvalLedger

    run_id = f"eval-learning-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"
    text = json.dumps({**result, "proposal": made.proposal_id}, sort_keys=True)
    digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    ledger = EvalLedger(rt.ws.repo, run_id, rt.ws.policy().version, made.agent)
    ledger("eval.run_started")
    verdict = "regressed" if result["regressed"] else "measured"
    ledger("eval.learning_measured", measurement=digest, verdict=verdict)
    return run_id


__all__ = ["MAX_REASON", "STEP", "measure_change", "overlay_block", "overlay_sha"]
