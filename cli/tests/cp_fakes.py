"""A fake control plane for the CLI tests: the `/v1` routes the CLI calls, answered from
memory through `httpx2.MockTransport`, every request recorded (headers, query, body)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2

from agentic.policy import tree_digest

CP_URL = "https://cp.acme.example"
TOKEN = "acp_" + "t" * 43  # the shape of a control-plane token, not a real one
VERSION = "git:" + "ab" * 20


def bundle_of(root: Path) -> bytes:
    """A policy repo folder as the control plane serves it: a tar, gzipped."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(root).as_posix())
    return gzip.compress(raw.getvalue(), mtime=0)


@dataclass
class FakeControlPlane:
    policy_root: Path | None = None
    tenant: str = "acme"
    email: str = "dev@acme.example"
    assignments: list[dict] = field(
        default_factory=lambda: [{"role": "developer", "scope": "team", "team": "payments"}]
    )
    token: str = TOKEN
    allowed: set[str] = field(default_factory=lambda: {"agent.edit.repo"})
    down: bool = False
    bundle_override: bytes | None = None
    tamper_bundle: bool = False  # serve bytes that do not match the advertised digest
    tree_digest_override: str | None = None
    requests: list[httpx2.Request] = field(default_factory=list)
    events: dict[str, list[dict]] = field(default_factory=dict)
    registered: list[dict] = field(default_factory=list)
    refuse_ingest: dict | None = None
    max_body: int | None = None  # bytes: a larger request body is answered 413 too_large
    max_entries: int | None = None  # entries per ingest: more is 413 ingest.too_large
    switches: list[dict] = field(default_factory=list)  # kill and pause switches (M2-06)
    lead_teams: set[str] = field(default_factory=set)  # teams the user holds killswitch.team in
    registrations: list[dict] = field(default_factory=list)  # POST /v1/runs bodies (M2-21)
    failing_checks: dict[str, str] = field(default_factory=dict)  # check -> why it fails
    min_cli_version: str | None = None  # registration answers 426 below it (M2-08)
    proxy_key: str | None = None  # the key a rotation hands out (M2-17); None: none held

    def add_switch(
        self, kind: str, value: str = "", *, mode: str = "kill", reason: str = "r", team=None
    ) -> dict:
        switch = {
            "id": f"00000000-0000-0000-0000-{len(self.switches) + 1:012d}",
            "scope_kind": kind,
            "scope_value": value,
            "team": value if kind == "team" else team,
            "mode": mode,
            "reason": reason,
            "created_by": "admin@acme.example",
            "created_at": datetime.now(UTC).isoformat(),
            "cleared_by": None,
            "cleared_at": None,
            "active": True,
        }
        self.switches.append(switch)
        return switch

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def bundle(self) -> bytes:
        if self.bundle_override is not None:
            return self.bundle_override
        assert self.policy_root is not None
        return bundle_of(self.policy_root)

    def active(self) -> dict:
        data = self.bundle()
        return {
            "version": VERSION,
            "branch": "main",
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "tree_digest": self.tree_digest_override
            or (tree_digest(self.policy_root) if self.policy_root else "sha256:0"),
            "activated_at": datetime.now(UTC).isoformat(),
            "commit_at": None,
            "bundle_path": f"/v1/policy/versions/{VERSION}/bundle",
        }

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.down:
            raise httpx2.ConnectError("connection refused", request=request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return _problem(401, "auth.invalid_token", "the token is not valid")
        path = request.url.path
        if path == "/v1/me":
            return httpx2.Response(200, json=self.me())
        if path == "/v1/policy/active":
            return httpx2.Response(200, json=self.active())
        if path.startswith("/v1/policy/versions/") and path.endswith("/bundle"):
            served = self.bundle() + (b"tampered" if self.tamper_bundle else b"")
            return httpx2.Response(
                200, content=served, headers={"content-type": "application/gzip"}
            )
        if path == "/v1/rbac/check":
            body = json.loads(request.content)
            allowed = body["permission"] in self.allowed
            return httpx2.Response(
                200, json={"allowed": allowed, "reason": "fake", "role": None, "scope": None}
            )
        if path == "/v1/repos":
            body = json.loads(request.content)
            self.registered.append(body)
            return httpx2.Response(
                201,
                json={
                    "repo_key": "github.com/acme/app",
                    "remote": body["remote"],
                    "team": body["team"],
                },
            )
        if path == "/v1/killswitch/state":
            active = [s for s in self.switches if s["active"]]
            return httpx2.Response(
                200,
                json={
                    "switches": active,
                    "checked_at": datetime.now(UTC).isoformat(),
                    "ttl_seconds": 10,
                },
            )
        if path == "/v1/killswitch" or path.startswith("/v1/killswitch/"):
            return self._killswitch(request)
        if path == "/v1/runs" and request.method == "POST":
            return self._register(request)
        if path == "/v1/proxy-keys/rotate" and request.method == "POST":
            if self.proxy_key is None:
                return _problem(
                    404,
                    "proxy_key.none",
                    "you hold no gateway key issued through the control plane",
                    extra={"hint": "redeem your invite or ask your org admin to invite you again"},
                )
            return httpx2.Response(
                200,
                json={"key": self.proxy_key, "key_id": "0123456789abcdef", "gateway_url": None},
            )
        if path.startswith("/v1/runs/") and path.endswith("/events"):
            if self.max_body is not None and len(request.content) > self.max_body:
                return _problem(413, "too_large", f"the body is over {self.max_body} bytes")
            return self._ingest(path.split("/")[3], json.loads(request.content)["entries"])
        return _problem(404, "not_found", "no such route or object")

    def _may_switch(self, team: str | None) -> bool:
        if "killswitch.global" in self.allowed:
            return True
        return team is not None and team in self.lead_teams

    def _killswitch(self, request: httpx2.Request) -> httpx2.Response:
        forbidden = _problem(403, "rbac.forbidden", "you do not hold killswitch.team for that")
        if request.method == "POST":
            body = json.loads(request.content)
            team = body.get("team") or (
                body.get("scope_value") if body["scope_kind"] == "team" else None
            )
            if not self._may_switch(team):
                return forbidden
            made = self.add_switch(
                body["scope_kind"],
                body.get("scope_value", ""),
                mode=body.get("mode", "kill"),
                reason=body["reason"],
                team=team,
            )
            return httpx2.Response(201, json=made)
        if request.method == "DELETE":
            wanted = request.url.path.rsplit("/", 1)[1]
            found = next((s for s in self.switches if s["id"] == wanted), None)
            if found is None:
                return _problem(404, "not_found", "no such switch")
            if not self._may_switch(found["team"]):
                return forbidden
            found.update(active=False, cleared_by=self.email, cleared_at=found["created_at"])
            return httpx2.Response(200, json=found)
        if not self._may_switch(None) and not self.lead_teams:
            return _problem(403, "rbac.forbidden", "you do not hold killswitch.team")
        include = request.url.params.get("include_cleared") == "true"
        shown = [
            s for s in self.switches if (include or s["active"]) and self._may_switch(s["team"])
        ]
        return httpx2.Response(200, json=shown)

    def _register(self, request: httpx2.Request) -> httpx2.Response:
        sent = request.headers.get("x-agentic-cli-version", "")
        if self.min_cli_version and sent < self.min_cli_version:
            body = {
                "title": "x",
                "status": 426,
                "code": "cli.too_old",
                "detail": f"agentic-cli {sent} is older than the minimum version "
                f"{self.min_cli_version}",
                "hint": f"upgrade the agentic CLI to {self.min_cli_version} or later",
            }
            return httpx2.Response(
                426, json=body, headers={"content-type": "application/problem+json"}
            )
        body = json.loads(request.content)
        names = ("rbac", "killswitch", "policy", "pipeline", "concurrency", "budget")
        checks = [
            {
                "check": name,
                "passed": name not in self.failing_checks,
                "detail": self.failing_checks.get(name, "ok"),
                "value": None,
            }
            for name in names
        ]
        registered = not self.failing_checks and not body.get("dry_run")
        if registered:
            self.registrations.append(body)
        return httpx2.Response(
            200,
            json={
                "registered": registered,
                "checks": checks,
                "team_budget_remaining_usd": None,
                "min_cli_version": self.min_cli_version,
            },
        )

    def me(self) -> dict:
        return {
            "user": {"id": "00000000-0000-0000-0000-000000000001", "email": self.email},
            "kind": "human",
            "tenant": {"slug": self.tenant, "name": self.tenant.title()},
            "token_expires_at": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
            "assignments": self.assignments,
        }

    def _ingest(self, run_id: str, entries: list[dict]) -> httpx2.Response:
        if self.refuse_ingest is not None:
            return httpx2.Response(
                self.refuse_ingest["status"],
                json=self.refuse_ingest,
                headers={"content-type": "application/problem+json"},
            )
        if self.max_entries is not None and len(entries) > self.max_entries:
            return _problem(413, "ingest.too_large", f"at most {self.max_entries} entries")
        stored = self.events.setdefault(run_id, [])
        head = len(stored)
        duplicates = [e for e in entries if e["seq"] <= head]
        rest = [e for e in entries if e["seq"] > head]
        if rest and rest[0]["seq"] != head + 1:
            last = stored[-1]["hash"] if stored else ""
            return _problem(409, "ingest.gap", "gap", extra={"head": {"seq": head, "hash": last}})
        stored.extend(rest)
        last = stored[-1]
        return httpx2.Response(
            200,
            json={
                "accepted": len(rest),
                "duplicates": len(duplicates),
                "head": {"seq": last["seq"], "hash": last["hash"]},
            },
        )


def _problem(status: int, code: str, detail: str, *, extra: dict | None = None) -> httpx2.Response:
    body = {"title": "x", "status": status, "detail": detail, "code": code, **(extra or {})}
    if status == 401:
        body["hint"] = "run `agentic login --for control-plane` to get a new token"
    return httpx2.Response(status, json=body, headers={"content-type": "application/problem+json"})
