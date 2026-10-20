"""TokenLean's X-Trace-ID is stored beside the model-call span (spec §17; M1-43)."""

import asyncio
from decimal import Decimal

import httpx2

from agentic.agents.models import GatewaySettings, metered_model
from agentic.agents.runner import AgentTask, PydanticAIRunner
from agentic.telemetry.spans import finish_step_span, step_span
from agentic.transport import BudgetLedger

GATEWAY = GatewaySettings(base_url="https://tokenlean.acme.internal", api_key="tok-gateway")
TRACE = "0af7651916cd43dd8448eb211c80319c"


def traced(tokenlean):
    def respond(request: httpx2.Request) -> httpx2.Response:
        response = tokenlean(request)
        response.headers["x-trace-id"] = TRACE
        response.headers["x-tokenlean-request-id"] = "req-7"
        return response

    return respond


def run_once(resolved, identity, tokenlean, tmp_path, *, wrap_step=False):
    agent = resolved.model_copy(update={"model": "openai/gpt-5", "temperature": None})
    budget = BudgetLedger(Decimal("5")).run.open("build-1", "step", Decimal("1"))
    model, log = metered_model(
        agent,
        identity,
        GATEWAY,
        budget=budget,
        price_for=lambda name: None,
        fallback_estimate_usd=Decimal("0.01"),
        _inner=httpx2.MockTransport(traced(tokenlean)),
    )
    runner = PydanticAIRunner(agent, model, budget=budget, state_dir=tmp_path, costs=log)
    task = AgentTask(instructions="Be brief.", inputs={}, prompt="hi")

    async def go():
        if not wrap_step:
            return await runner.run(task)
        with step_span("r-sp4", "build", 1, agent) as span:
            outcome = await runner.run(task)
            finish_step_span(span, outcome)
            return outcome

    return asyncio.run(go())


def test_tokenlean_trace_id_is_recorded_on_the_model_call_span(
    resolved, identity, tokenlean, tmp_path, spans
):
    outcome = run_once(resolved, identity, tokenlean, tmp_path)
    assert outcome.status == "completed"
    (call,) = [s for s in spans.get_finished_spans() if s.name.startswith("chat ")]
    assert call.attributes["agentic.tokenlean.trace_id"] == TRACE
    assert call.attributes["agentic.tokenlean.request_id"] == "req-7"
    assert call.attributes["agentic.cost_source"] in ("reported", "estimated")
    assert outcome.costs.calls[0][0].trace_id == TRACE  # and in the run's cost log


def test_step_span_lists_the_steps_tokenlean_trace_ids(
    resolved, identity, tokenlean, tmp_path, spans
):
    run_once(resolved, identity, tokenlean, tmp_path, wrap_step=True)
    (step,) = [s for s in spans.get_finished_spans() if s.name.startswith("step ")]
    assert tuple(step.attributes["agentic.tokenlean.trace_ids"]) == (TRACE,)
    assert step.attributes["agentic.cost_usd"] == "0.004321"
    assert step.attributes["gen_ai.aggregated_usage.input_tokens"] == 1200
