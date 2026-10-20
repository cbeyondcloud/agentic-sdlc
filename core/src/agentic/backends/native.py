"""The native backend: Pydantic AI, every call through TokenLean (M1-28).

The provider-agnostic default: the resolved model (any provider TokenLean routes -
Anthropic, OpenAI, Google, local models) is called through the identity client,
each call metered against the step's budget slice. It honours every capability:
all tools, child agents, approval pauses, the stuck check.
"""

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
from pydantic_ai.models import Model

from agentic.agents.metering import CostLog
from agentic.agents.models import GatewaySettings, metered_model
from agentic.agents.runner import PydanticAIRunner
from agentic.agents.tools import ToolContext, build_tools, protect_tools
from agentic.security import SecretScanner
from agentic.transport import CallIdentity, Pricer

if TYPE_CHECKING:
    from agentic.agents.children import ChildSpawner
    from agentic.engine.engine import StepContext


class NativeBackend:
    name = "native"

    def __init__(
        self,
        *,
        identity_for: Callable[["StepContext"], CallIdentity],
        gateway: GatewaySettings,
        prices: Pricer,
        fallback_estimate_usd: Decimal,
        model_for: Callable[["StepContext"], Model] | None = None,  # tests: a stand-in model
        policy_version: str | None = None,
        network_transport: httpx2.AsyncBaseTransport | None = None,  # tests: no network
        # M1-41: the step's secret scanner (policy action, audit sink); None = block
        scanner_for: Callable[["StepContext"], SecretScanner] | None = None,
    ) -> None:
        self.identity_for, self.gateway, self.prices = identity_for, gateway, prices
        self.fallback_estimate_usd = fallback_estimate_usd
        self.model_for, self.policy_version = model_for, policy_version
        self.network_transport, self.scanner_for = network_transport, scanner_for

    def runner(
        self,
        step: "StepContext",
        *,
        tool_context: ToolContext | None,
        state_dir: Path,
        spawner: "ChildSpawner | None",
        is_child: bool = False,
    ) -> PydanticAIRunner:
        costs = CostLog()
        scanner = self.scanner_for(step) if self.scanner_for is not None else None
        if self.model_for is not None:
            model = self.model_for(step)
        else:
            model, costs = metered_model(
                step.agent,
                self.identity_for(step),
                self.gateway,
                budget=step.budget,
                price_for=self.prices,
                fallback_estimate_usd=self.fallback_estimate_usd,
                log=costs,
                scanner=scanner,
                _inner=self.network_transport,
            )
        if tool_context is not None and tool_context.secret_scanner is None:
            tool_context = replace(tool_context, secret_scanner=scanner)  # the step's policy
        tools = build_tools(step.agent, tool_context) if tool_context is not None else []
        if spawner is not None:  # a child's answer was shaped by what it read: data too
            tools += protect_tools(
                spawner.tools(),
                scanner=tool_context.secret_scanner if tool_context else scanner,
                tree=tool_context.tree if tool_context else None,
            )
        return PydanticAIRunner(
            step.agent,
            model,
            budget=step.budget,
            state_dir=state_dir,
            tools=tools,
            costs=costs,
            is_child=is_child,
            contract=step.contract,
            run_id=step.run_id,
            policy_version=self.policy_version,
            scanner=scanner,
        )
