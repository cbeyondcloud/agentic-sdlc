"""A fake of TokenLean's `/admin/routing` for the control-plane tests (no live gateway):
the `GatewayRouting` port over a versioned in-memory overlay, with TokenLean's rules - only
its allowed paths, `expected_version` or 409, a model it does not serve is 422 with the
rule's path - plus switches for an unreachable gateway, a concurrent writer, a read-back
that differs, and a hold point that keeps one call waiting until the test releases it."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field

from agentic_cp.keys.gateway import GatewayUnavailable
from agentic_cp.routing.gateway import RoutingConflict, RoutingRefused, RoutingState

TOP = {"groups", "rate_limit", "teams", "tenants"}
TENANT = {"groups", "teams"}


@dataclass
class FakeRouting:
    configured: bool = True
    down: bool = False
    versions: list[dict] = field(default_factory=lambda: [{}])  # version n is versions[n]
    comments: list[str] = field(default_factory=lambda: [""])
    calls: list[str] = field(default_factory=list)
    unknown_models: set[str] = field(default_factory=set)
    """Models no configured provider offers: a rule naming one is refused (422)."""
    interleave: list[dict] = field(default_factory=list)
    """Fragments another writer stores just before each of our next writes (a 409 each)."""
    tamper_readback: bool = False
    hold: str | None = None
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def version(self) -> int:
        return len(self.versions) - 1

    @property
    def current(self) -> dict:
        return self.versions[-1]

    def store(self, fragment: dict, comment: str = "operator") -> int:
        self.versions.append(copy.deepcopy(fragment))
        self.comments.append(comment)
        return self.version

    async def _enter(self, call: str) -> None:
        self.calls.append(call)
        if self.down:
            raise GatewayUnavailable("the gateway is unreachable (fake)")
        if call == self.hold:
            self.entered.set()
            await self.release.wait()

    async def read(self) -> RoutingState:
        await self._enter("read")
        fragment = copy.deepcopy(self.current)
        if self.tamper_readback and self.calls.count("write"):
            fragment.setdefault("tenants", {})["tampered"] = {}
        return RoutingState(self.version, fragment)

    async def read_version(self, version: int) -> RoutingState | None:
        await self._enter("read_version")
        if not 0 <= version <= self.version:
            return None
        return RoutingState(version, copy.deepcopy(self.versions[version]))

    async def write(self, fragment: dict, *, expected_version: int, comment: str) -> RoutingState:
        await self._enter("write")
        if self.interleave:
            self.store(self.interleave.pop(0), "another writer")
        if expected_version != self.version:
            raise RoutingConflict(self.version)
        if errors := self._problems(fragment):
            raise RoutingRefused(errors)
        self.store(fragment, comment)
        return RoutingState(self.version, copy.deepcopy(fragment))

    def _problems(self, fragment: dict) -> list[str]:
        errors = [f"{key}: not allowed in the routing overlay" for key in set(fragment) - TOP]
        for tenant, block in (fragment.get("tenants") or {}).items():
            errors += [f"tenants.{tenant}.{key}: not allowed" for key in set(block) - TENANT]
            rules = (block.get("groups") or {}).get("G6_routing", {}).get("rules", [])
            for n, rule in enumerate(rules):
                model = rule.get("action", {}).get("model")
                if model in self.unknown_models:
                    path = f"tenants.{tenant}.groups.G6_routing.rules[{n}].action.model"
                    errors.append(f"{path}: model {model!r} is not offered by any provider")
        if set(fragment.get("rate_limit") or {}) - {"per_team"}:
            errors.append("rate_limit: only per_team may be set")
        return errors
