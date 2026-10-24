"""A fake control plane for the CLI tests: the `/v1` routes the CLI calls, answered from
memory through `httpx2.MockTransport`, every request recorded (headers, query, body)."""

from __future__ import annotations

import csv
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
    invites: dict[str, dict] = field(default_factory=dict)  # token -> redemption (M2-18)
    used_invites: set[str] = field(default_factory=set)
    role_assignments: list[dict] = field(default_factory=list)  # `agentic rbac` (M2-25)
    imports: list[dict] = field(default_factory=list)  # bodies of POST /v1/rbac/import
    import_rows: list[dict] = field(default_factory=list)  # what an import answers
    offboarded: list[tuple[str, str]] = field(default_factory=list)
    keys_revoked: list[str] = field(default_factory=list)  # DELETE /v1/users/<e>/proxy-keys
    service_accounts: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)  # the tenant's audit chain (M2-26)
    export_cut: bool = False  # the export stream stops before its proof line (M2-13)

    def add_audit(self, count: int, *, team_of=lambda seq: "payments") -> list[dict]:
        """`count` chained audit records appended to the tenant's chain."""
        from agentic.auditproof import entry_hash

        for _ in range(count):
            seq = len(self.audit) + 1
            body = {
                "seq": seq,
                "tenant": "00000000-0000-4000-8000-0000000000aa",
                "team": team_of(seq),
                "actor": "lead@acme.example",
                "actor_kind": "human",
                "event": "killswitch.set",
                "target": f"killswitch:{seq}",
                "before": None,
                "after": {"mode": "pause"},
                "reason": f"reason {seq}",
                "run_id": None,
                "trace_id": None,
                "policy_version": None,
                "at": f"2026-10-{1 + seq % 28:02d}T09:00:00.000Z",
                "prev": self.audit[-1]["hash"] if self.audit else "",
            }
            self.audit.append({**body, "hash": entry_hash(body)})
        return self.audit

    def _audit(self, request: httpx2.Request) -> httpx2.Response:
        params = request.url.params
        if request.url.path == "/v1/audit/export":
            return self._export(request)
        if "audit.read.org" not in self.allowed:
            return _problem(403, "rbac.forbidden", "you do not hold audit.read.org")
        after, limit = int(params.get("after_seq", "0")), int(params.get("limit", "100"))
        page = [r for r in self.audit if r["seq"] > after][:limit]
        return httpx2.Response(200, json=page)

    def _export(self, request: httpx2.Request) -> httpx2.Response:
        """The export as the control plane streams it: every record in the range, other
        teams' as link rows, then the proof (unless `export_cut`)."""
        from agentic import auditproof

        if "audit.export" not in self.allowed:
            return _problem(403, "rbac.forbidden", "you do not hold audit.export")
        team, fmt = request.url.params.get("team"), request.url.params.get("format", "jsonl")
        records = self.audit
        start = (records[0]["seq"] - 1, records[0]["prev"]) if records else None
        rows = [
            auditproof.header_row(
                tenant=records[0]["tenant"] if records else "",
                team=team,
                start=start,
                exported_by=self.email,
            )
        ]
        entries = links = 0
        for r in records:
            if team is not None and r["team"] != team:
                rows.append(auditproof.link_row(r))
                links += 1
            else:
                rows.append(auditproof.entry_row(r))
                entries += 1
        end = (records[-1]["seq"], records[-1]["hash"]) if records else None
        if not self.export_cut:
            rows.append(auditproof.proof_row(entries=entries, links=links, end=end, head=end))
        if fmt == "csv":
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(auditproof.CSV_COLUMNS)
            for row in rows:
                if row["kind"] in ("header", "proof"):
                    writer.writerow(auditproof.csv_meta_row(row))
                elif row["kind"] == "link":
                    writer.writerow(auditproof.csv_row("link", row))
                else:
                    writer.writerow(auditproof.csv_row("entry", row["record"]))
            body = out.getvalue().encode("utf-8")
            return httpx2.Response(200, content=body, headers={"content-type": "text/csv"})
        body = b"".join(auditproof.jsonl_line(row) for row in rows)
        return httpx2.Response(200, content=body, headers={"content-type": "application/x-ndjson"})

    def add_invite(self, token: str, *, gateway_key: str | None = None) -> dict:
        self.invites[token] = {
            "tenant": {"slug": self.tenant, "name": self.tenant.title()},
            "email": self.email,
            "control_plane_url": CP_URL,
            "control_plane_token": self.token,
            "token_expires_at": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
            "gateway_url": "https://gateway.acme.example" if gateway_key else None,
            "gateway_key": gateway_key,
            "gateway_key_id": "0123456789abcdef" if gateway_key else None,
            "notice": None if gateway_key else "no gateway key from this control plane",
        }
        return self.invites[token]

    # ── RBAC administration (M2-25): enough of /v1/rbac and friends for `agentic rbac` ──

    def _may_assign(self, team: str | None) -> httpx2.Response | None:
        """None when allowed: org admins anywhere, team leads in their teams."""
        if "rbac.assign" in self.allowed or (team is not None and team in self.lead_teams):
            return None
        where = f"team {team}" if team else "the org"
        need = "rbac.assign.team" if team else "rbac.assign"
        return _problem(403, "rbac.forbidden", f"you do not hold {need} for {where}")

    def _rbac(self, request: httpx2.Request) -> httpx2.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        roles = [
            {"name": "developer", "permissions": ["run.trigger"], "builtin": True},
            {"name": "team-lead", "permissions": ["rbac.assign.team"], "builtin": True},
        ]
        for role in roles:
            role["team_assignable"] = role["name"] == "developer"
        if path == "/v1/rbac/roles":
            return httpx2.Response(200, json=roles)
        if path.startswith("/v1/rbac/roles/"):
            found = [r for r in roles if r["name"] == path.rsplit("/", 1)[1]]
            return (
                httpx2.Response(200, json=found[0])
                if found
                else _problem(404, "not_found", "no role")
            )
        if path == "/v1/rbac/assignments" and method == "GET":
            team, user = request.url.params.get("team"), request.url.params.get("user")
            if refused := self._may_assign(team):
                return refused
            shown = [
                a
                for a in self.role_assignments
                if (team is None or a["team"] == team) and (user is None or a["email"] == user)
            ]
            return httpx2.Response(200, json=shown)
        if path == "/v1/rbac/assignments" and method == "POST":
            if refused := self._may_assign(body.get("team")):
                return refused
            made = {
                "id": f"00000000-0000-0000-0001-{len(self.role_assignments) + 1:012d}",
                "email": body["email"],
                "role": body["role"],
                "scope": "team" if body.get("team") else "org",
                "team": body.get("team"),
                "invited": True,
            }
            self.role_assignments.append(made)
            return httpx2.Response(201, json=made)
        if path.startswith("/v1/rbac/assignments/") and method == "DELETE":
            wanted = path.rsplit("/", 1)[1]
            found = [a for a in self.role_assignments if a["id"] == wanted]
            if not found:
                return _problem(404, "not_found", "no such assignment")
            if refused := self._may_assign(found[0]["team"]):
                return refused
            self.role_assignments.remove(found[0])
            return httpx2.Response(204)
        if path == "/v1/rbac/import":
            refused = self._may_assign(None)
            if refused is not None and not self.lead_teams:
                return refused
            self.imports.append(body)
            rows = self.import_rows
            errors = [row for row in rows if row["action"] == "error"]
            if errors and not body["dry_run"]:
                problems = [f"line {row['line']}: {row['error']}" for row in errors]
                return _problem(
                    422, "rbac.import_invalid", "rows have errors", extra={"problems": problems}
                )
            result = {
                "applied": not body["dry_run"],
                "added": sum(row["action"] == "add" for row in rows),
                "unchanged": sum(row["action"] == "unchanged" for row in rows),
                "errors": len(errors),
                "rows": rows,
            }
            return httpx2.Response(200, json=result)
        if path == "/v1/service-accounts" and method == "POST":
            if refused := self._may_assign(body["team"]):
                return refused
            made = {
                "id": f"00000000-0000-0000-0002-{len(self.service_accounts) + 1:012d}",
                "email": f"{body['name']}@{body['team']}.service.invalid",
                "team": body["team"],
            }
            self.service_accounts.append(
                {**made, "status": "active", "created_at": datetime.now(UTC).isoformat()}
            )
            return httpx2.Response(
                201,
                json={
                    **made,
                    "token": "acp_" + "s" * 43,
                    "token_id": "00000000-0000-0000-0003-000000000001",
                    "expires_at": (datetime.now(UTC) + timedelta(days=body["days"])).isoformat(),
                },
            )
        if path == "/v1/service-accounts" and method == "GET":
            team = request.url.params.get("team")
            if refused := self._may_assign(team):
                return refused
            shown = [a for a in self.service_accounts if team is None or a["team"] == team]
            return httpx2.Response(200, json=shown)
        if path == "/v1/notifications":
            if "audit.read.org" not in self.allowed:
                return _problem(403, "rbac.forbidden", "you do not hold audit.read.org")
            status = request.url.params.get("status")
            now = datetime.now(UTC).isoformat()
            mails = [
                {
                    "id": f"00000000-0000-0000-0006-00000000000{n}",
                    "kind": "invite",
                    "recipient": f"p{n}@acme.example",
                    "subject": "You are invited",
                    "status": state,
                    "attempts": attempts,
                    "next_attempt_at": now,
                    "last_error": error,
                    "created_at": now,
                    "sent_at": now if state == "sent" else None,
                }
                for n, (state, attempts, error) in enumerate(
                    [("sent", 1, None), ("failed", 1, "550 no such user here")], start=1
                )
            ]
            return httpx2.Response(
                200, json=[m for m in mails if status is None or m["status"] == status]
            )
        if path.endswith("/proxy-keys") and method == "DELETE":
            if refused := self._may_assign(None):
                return refused
            email = path.split("/")[3]
            self.keys_revoked.append(email)
            return httpx2.Response(
                200,
                json={
                    "email": email,
                    "revoked": ["0123456789abcdef"],
                    "pending": ["fedcba9876543210"],
                },
            )
        if path.endswith("/invite") or path.endswith("/offboard"):
            email = path.split("/")[3]
            if refused := self._may_assign(None):
                return refused
            if path.endswith("/invite"):
                expires = (datetime.now(UTC) + timedelta(hours=72)).isoformat()
                return httpx2.Response(
                    201,
                    json={
                        "id": "00000000-0000-0000-0004-000000000001",
                        "email": email,
                        "expires_at": expires,
                        "mailed": True,
                    },
                )
            self.offboarded.append((email, body["reason"]))
            return httpx2.Response(
                200,
                json={
                    "email": email,
                    "tokens_revoked": 1,
                    "invites_revoked": 0,
                    "proxy_keys_revoked": ["0123456789abcdef"],
                    "proxy_keys_pending": [],
                    "kill_switch": "00000000-0000-0000-0005-000000000001",
                },
            )
        return _problem(404, "not_found", "no such route or object")

    def _redeem(self, request: httpx2.Request) -> httpx2.Response:
        token = json.loads(request.content)["token"]
        hint = {"hint": "ask your team lead or org admin to send you a new invite"}
        if token in self.used_invites:
            return _problem(409, "invite.used", "this invite link was already used", extra=hint)
        if token not in self.invites:
            return _problem(404, "invite.invalid", "the invite link is not valid", extra=hint)
        self.used_invites.add(token)
        return httpx2.Response(200, json=self.invites[token])

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
        if request.url.path == "/v1/invites/redeem" and request.method == "POST":
            return self._redeem(request)  # the one route without a bearer token
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
        if path.startswith(
            ("/v1/rbac/", "/v1/service-accounts", "/v1/users/", "/v1/notifications")
        ):
            return self._rbac(request)
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
        if path == "/v1/audit" or path == "/v1/audit/export":
            return self._audit(request)
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
        if path.startswith("/v1/personal/devices/") and path.endswith("/events"):
            device = path.split("/")[4]
            entries = json.loads(request.content)["entries"]
            return self._ingest(f"personal:{device}", entries)
        if path == "/v1/personal/files":
            return httpx2.Response(200, json=self.personal_files())
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

    def personal(self) -> list[dict]:
        """Every personal-backup event received, over all devices, in arrival order."""
        found = [(key, e) for key, events in self.events.items() for e in events]
        return [e for key, e in found if key.startswith("personal:")]

    def personal_files(self) -> list[dict]:
        """`GET /v1/personal/files`: the newest saved-or-removed copy per path."""
        newest: dict[str, dict] = {}
        for key, events in self.events.items():
            if not key.startswith("personal:"):
                continue
            for entry in events:
                if entry["event"] == "personal.held":
                    newest.setdefault(entry["path"], {}).update(held=True)
                    continue
                newest[entry["path"]] = {
                    "kind": entry["kind"],
                    "path": entry["path"],
                    "sha256": entry["sha256"],
                    "content": entry.get("content"),
                    "deleted": entry["event"] == "personal.removed",
                    "held": False,
                    "device": key.split(":", 1)[1],
                    "seq": entry["seq"],
                    "at": entry["at"],
                }
        return [item for item in newest.values() if "kind" in item]

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
