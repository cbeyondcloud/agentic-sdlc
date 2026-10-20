"""Spans for agent steps and model calls (M1-43; spec §14.1, §17).

Only the OpenTelemetry API is used here: without an SDK installed and configured
(`agentic.telemetry.sdk`, extra `agentic-core[telemetry]`) every span is a no-op.

  * `step_span` / `finish_step_span` - the engine's span around one agent step;
  * `model_instrumentation` - the settings Pydantic AI instruments the native
    backend's model with: its `invoke_agent` and `chat` spans, prompts and answers
    left out (D14: capture is off by default; OR-01);
  * `mark_call` - TokenLean's `X-Trace-ID` and request id, set on the span that is
    current while the call settles (the model-call span), so the framework's trace
    and TokenLean's records can be joined until TokenLean reads `traceparent` (ST-3).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from pydantic_ai.models.instrumented import InstrumentationSettings

from agentic.telemetry.semconv import INSTRUMENTATION_VERSION, SCHEMA_URL

if TYPE_CHECKING:
    from agentic.agents.runner import AgentOutcome
    from agentic.policy import ResolvedAgent
    from agentic.transport import CallCost

TRACER_NAME = "agentic"


def tracer() -> trace.Tracer:
    """The framework's tracer, from the global provider at call time."""
    return trace.get_tracer(TRACER_NAME, schema_url=SCHEMA_URL)


def model_instrumentation() -> InstrumentationSettings:
    """How Pydantic AI instruments a model: pinned data format, no content."""
    return InstrumentationSettings(
        include_content=False,
        include_binary_content=False,
        include_model_request_parameters=False,
        version=INSTRUMENTATION_VERSION,
    )


@contextmanager
def step_span(run_id: str, step: str, iteration: int, agent: "ResolvedAgent") -> Iterator[Span]:
    """The span of one agent step (a run or a resume). An exception leaving it marks the
    span as an error naming only the exception's type (`error.type`, and the status
    description): its message may echo a provider's response, a prompt or a key."""
    name, version = agent.definition.name, agent.definition.version
    attributes = {
        "gen_ai.agent.name": name,
        "gen_ai.agent.id": f"{name}@{version}",
        "gen_ai.request.model": agent.model,
        "agentic.run_id": run_id,
        "agentic.step": step,
        "agentic.iteration": iteration,
        "agentic.agent.version": version,
    }
    with tracer().start_as_current_span(
        f"step {step}",
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as error:
            kind = type(error).__name__
            span.set_attribute("error.type", kind)
            span.set_status(Status(StatusCode.ERROR, kind))
            raise


def finish_step_span(span: Span, outcome: "AgentOutcome") -> None:
    """What the step came to: its outcome, usage, cost and TokenLean trace ids."""
    if not span.is_recording():
        return
    span.set_attribute("agentic.outcome", outcome.status)
    if outcome.stopped:
        span.set_attribute("agentic.stopped", outcome.stopped)
    usage = outcome.usage
    span.set_attribute("gen_ai.aggregated_usage.input_tokens", usage.input_tokens)
    span.set_attribute("gen_ai.aggregated_usage.output_tokens", usage.output_tokens)
    span.set_attribute("agentic.cost_usd", str(outcome.costs.total_usd()))
    traces = [cost.trace_id for cost, _ in outcome.costs.calls if cost.trace_id]
    if traces:
        span.set_attribute("agentic.tokenlean.trace_ids", list(dict.fromkeys(traces)))
    if outcome.status in ("stopped", "invalid"):
        # the reason code, never `detail`: a stuck pattern quotes the agent's tool calls
        span.set_status(Status(StatusCode.ERROR, outcome.stopped or outcome.status))


def mark_call(cost: "CallCost") -> None:
    """Record one settled call's TokenLean ids and cost on the current span."""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    if cost.trace_id:
        span.set_attribute("agentic.tokenlean.trace_id", cost.trace_id)
    if cost.request_id:
        span.set_attribute("agentic.tokenlean.request_id", cost.request_id)
    span.set_attribute("agentic.cost_source", cost.source)
    if cost.cost_usd is not None:
        span.set_attribute("agentic.cost_usd", str(cost.cost_usd))
