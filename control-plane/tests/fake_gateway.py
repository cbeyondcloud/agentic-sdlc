"""A fake gateway key store for the control-plane tests (no live TokenLean): the
`GatewayKeys` port in memory, with TokenLean's key ids (the first 16 hex characters of the
key's sha256), a switch that makes it unreachable, and a hold point that keeps one kind of
call waiting (a slow gateway) until the test releases it."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass, field

from agentic_cp.keys.gateway import GatewayUnavailable, IssuedKey


@dataclass
class FakeKey:
    key: str
    tenant: str
    team: str | None
    user: str
    label: str
    revoked: bool = False


@dataclass
class FakeGateway:
    configured: bool = True
    down: bool = False
    keys: dict[str, FakeKey] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    hold: str | None = None
    """A call name (`create`, `revoke`, `rotate`) that waits for `release` once entered."""
    hold_skip: int = 0
    """Calls of that name let through before the hold (to hold the second of two)."""
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def _wait(self, call: str) -> None:
        if call != self.hold:
            return
        if self.hold_skip > 0:
            self.hold_skip -= 1
            return
        self.entered.set()
        await self.release.wait()

    def _new(self, tenant: str, team: str | None, user: str, label: str) -> IssuedKey:
        key = "tlk-" + secrets.token_hex(20)  # a made-up shape, not a real key format
        key_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        self.keys[key_id] = FakeKey(key, tenant, team, user, label)
        return IssuedKey(key=key, key_id=key_id)

    def _check(self, call: str, key_id: str = "") -> None:
        self.calls.append((call, key_id))
        if self.down:
            raise GatewayUnavailable("the gateway is unreachable (fake)")

    async def create(self, *, tenant: str, team: str | None, user: str, label: str) -> IssuedKey:
        self._check("create")
        await self._wait("create")
        return self._new(tenant, team, user, label)

    async def revoke(self, key_id: str) -> None:
        self._check("revoke", key_id)
        await self._wait("revoke")
        if key_id in self.keys:
            self.keys[key_id].revoked = True

    async def rotate(self, key_id: str) -> IssuedKey:
        self._check("rotate", key_id)
        await self._wait("rotate")
        old = self.keys[key_id]
        old.revoked = True
        return self._new(old.tenant, old.team, old.user, old.label)

    def accepts(self, key: str) -> bool:
        """Would the gateway serve a request made with `key`?"""
        return any(k.key == key and not k.revoked for k in self.keys.values())

    def active_for(self, user: str) -> list[FakeKey]:
        return [k for k in self.keys.values() if k.user == user and not k.revoked]
