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
        if path.startswith("/v1/runs/") and path.endswith("/events"):
            if self.max_body is not None and len(request.content) > self.max_body:
                return _problem(413, "too_large", f"the body is over {self.max_body} bytes")
            return self._ingest(path.split("/")[3], json.loads(request.content)["entries"])
        return _problem(404, "not_found", "no such route or object")

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
