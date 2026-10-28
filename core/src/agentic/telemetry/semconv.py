"""The span attributes the framework promises, pinned (M1-43; spec §14.1).

Three kinds of span make up a step's trace:

  * step       - the engine's span for one agent step (`step <id>`), whatever the
                 backend: the agent's gen_ai identity plus the framework's ids;
  * agent_run  - Pydantic AI's `invoke_agent <agent>` span (native backend);
  * model_call - Pydantic AI's `chat <model>` span, one per model request.

The gen_ai names follow OpenTelemetry's GenAI semantic conventions at
`SEMCONV_VERSION` (the version that renamed `gen_ai.system` to
`gen_ai.provider.name`), as emitted by the pinned pydantic-ai-slim with data format
`INSTRUMENTATION_VERSION`. A dependency bump that changes them fails
`span_problems` in the tests, so dashboards never break silently.

Usage on the step span uses Pydantic AI's `gen_ai.aggregated_usage.*` names: its
model-call children already carry `gen_ai.usage.*`, and a backend summing span
attributes would otherwise count every token twice.
"""

from collections.abc import Mapping
from typing import Literal

SEMCONV_VERSION = "1.37.0"
SCHEMA_URL = f"https://opentelemetry.io/schemas/{SEMCONV_VERSION}"
#: Pydantic AI's instrumentation data format (InstrumentationSettings.version).
INSTRUMENTATION_VERSION = 5

SpanKind = Literal["step", "agent_run", "model_call"]

STEP_SPAN: Mapping[str, type] = {
    "gen_ai.agent.name": str,
    "gen_ai.agent.id": str,  # name@version
    "gen_ai.request.model": str,  # the resolved "<provider>/<model>"
    "agentic.run_id": str,
    "agentic.step": str,
    "agentic.iteration": int,
    "agentic.agent.version": str,
}
AGENT_RUN_SPAN: Mapping[str, type] = {
    "gen_ai.operation.name": str,
    "gen_ai.agent.name": str,
}
MODEL_CALL_SPAN: Mapping[str, type] = {
    "gen_ai.operation.name": str,
    "gen_ai.agent.name": str,
    "gen_ai.request.model": str,
    "gen_ai.provider.name": str,
    "gen_ai.usage.input_tokens": int,
    "gen_ai.usage.output_tokens": int,
}
SCHEMAS: Mapping[SpanKind, Mapping[str, type]] = {
    "step": STEP_SPAN,
    "agent_run": AGENT_RUN_SPAN,
    "model_call": MODEL_CALL_SPAN,
}
#: The `gen_ai.operation.name` each kind must carry, where the conventions name one.
OPERATIONS: Mapping[SpanKind, str] = {"agent_run": "invoke_agent", "model_call": "chat"}


def span_problems(kind: SpanKind, attributes: Mapping[str, object]) -> list[str]:
    """Why a span's attributes do not match the pinned schema for `kind` ([] = they do)."""
    problems = []
    for name, wanted in SCHEMAS[kind].items():
        if name not in attributes:
            problems.append(f"{name}: missing")
            continue
        value = attributes[name]
        if not isinstance(value, wanted) or (isinstance(value, bool) and wanted is int):
            problems.append(f"{name}: expected {wanted.__name__}, got {type(value).__name__}")
    operation = OPERATIONS.get(kind)
    if operation and attributes.get("gen_ai.operation.name", operation) != operation:
        found = attributes["gen_ai.operation.name"]
        problems.append(f"gen_ai.operation.name: expected {operation!r}, got {found!r}")
    return problems
