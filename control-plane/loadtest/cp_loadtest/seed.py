"""The load test's org: tenants, teams, people with tokens and roles, and an accepted policy
(M3-22).

Everything goes through the control plane's own code: tenants through `bootstrap_tenant`
(as `admin tenant create` does), people and their tokens through `create_user` and
`issue_token` on the owner role inside the tenant (as the tests' `add_user` does), the
policy through `PUT /v1/tenant/policy-source` (a real git sync and validation) and the
roles through `POST /v1/rbac/import` - one CSV per tenant, timed, which is DF-569's
measurement. The API calls go to the app in this process; the load itself goes over HTTP.

The policy repo is the standalone template's policy with every team, generous concurrency
caps (the load test measures the server, not the caps) and the library's
`feature-delivery` pipeline, whose bytes the simulated CLIs hash for registration (D33)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx2
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic.orgapi import API_PREFIX, CLI_VERSION_HEADER
from agentic.policy.load import pipeline_digests
from agentic_cp.app import create_app
from agentic_cp.auth.bootstrap import bootstrap_tenant
from agentic_cp.auth.tokens import issue_token
from agentic_cp.auth.users import create_user
from agentic_cp.settings import Settings
from agentic_cp.tenancy.context import tenant_tx

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_POLICY = REPO_ROOT / "templates" / "standalone" / "policy"
PIPELINE = "feature-delivery"
PIPELINE_SOURCE = REPO_ROOT / "library" / "pipelines" / f"{PIPELINE}.yaml"
PIPELINE_FILE = f"pipelines/{PIPELINE}.yaml"
AGENTS = ("business-analyst", "code-review", "design", "developer", "qa", "qa-baseline")
MODELS = ("acme/model-b", "acme/model-d", "acme/model-f")
TIERS = (
    "tiers:\n  deep-reasoning: { model: acme/model-d }\n  balanced: { model: acme/model-b }\n"
    "  fast: { model: acme/model-f }\n  local: { model: local/model-l }\n"
)
_GIT = [
    "git",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "user.name=Load Test",
    "-c",
    "user.email=loadtest@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "init.defaultBranch=main",
]


@dataclass(frozen=True)
class SimUser:
    tenant: str
    team: str
    email: str
    token: str

    def __repr__(self) -> str:  # never the token
        return f"SimUser({self.tenant}/{self.team}/{self.email})"


@dataclass
class Population:
    users: list[SimUser]
    tenant_ids: dict[str, UUID]
    policy_version: dict[str, str]
    pipeline_sha256: str
    import_seconds: list[float] = field(default_factory=list)


def team_names(teams: int) -> list[str]:
    return [f"team-{n:02d}" for n in range(1, teams + 1)]


def make_policy_repo(path: Path, teams: list[str], users_per_team: int) -> Path:
    """The policy repo (a local git repo on `main`), committed with LF bytes."""
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    for source in sorted(TEMPLATE_POLICY.iterdir()):
        if source.is_file():
            _write(path / "policy" / source.name, source.read_bytes().replace(b"\r\n", b"\n"))
    _write(path / "policy" / "tiers.yaml", TIERS.encode())
    caps = f"concurrency: {{ per_user: 3, per_team: {max(20, 2 * users_per_team)} }}\n"
    _write(path / "policy" / "limits.yaml", caps.encode())
    for team in teams:
        _write(path / "policy" / "teams" / f"{team}.yaml", b"{}\n")
    _write(path / PIPELINE_FILE, PIPELINE_SOURCE.read_bytes().replace(b"\r\n", b"\n"))
    for agent in AGENTS:  # the pipeline's agents, from the library as an org layer
        source = REPO_ROOT / "library" / "agents" / f"{agent}.md"
        _write(path / "agents" / f"{agent}.md", source.read_bytes().replace(b"\r\n", b"\n"))
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "load test policy")
    return path


async def seed(
    owner_engine: AsyncEngine,
    app_engine: AsyncEngine,
    settings: Settings,
    *,
    tenants: int,
    teams: int,
    users: int,
    workdir: Path,
    clock: Callable[[], datetime],
) -> Population:
    """`tenants` tenants (`lt-01`, ...) of `teams` teams each (`team-01`, ...), with `users`
    people spread evenly over every team of every tenant, each holding `developer` in their
    team and a 30-day token. `settings` must allow local policy repos."""
    names = team_names(teams)
    per_team = max(1, -(-users // (tenants * teams)))
    repo = make_policy_repo(workdir / "policy", names, per_team)
    digest = pipeline_digests(repo)[PIPELINE_FILE]
    population = Population([], {}, {}, digest)
    app = create_app(settings, engine=app_engine, clock=clock)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://seed.local") as http:
        for t in range(1, tenants + 1):
            slug = f"lt-{t:02d}"
            made = await bootstrap_tenant(
                owner_engine,
                settings,
                slug=slug,
                name=f"Load test {t}",
                admin_email=f"admin@{slug}.example.test",
                clock=clock,
            )
            population.tenant_ids[slug] = made.tenant.id
            admin = {"Authorization": f"Bearer {made.token}", CLI_VERSION_HEADER: "0.0.0"}
            synced = await http.put(
                f"{API_PREFIX}/tenant/policy-source",
                json={"repo": repo.resolve().as_uri(), "branch": "main"},
                headers=admin,
                timeout=120,
            )
            if synced.status_code != 200 or synced.json()["sync"]["kind"] != "activated":
                raise RuntimeError(f"policy sync for {slug} failed: {synced.text[:500]}")
            population.policy_version[slug] = synced.json()["sync"]["version"]
            mine = _people(slug, t, tenants, names, users)
            async with tenant_tx(owner_engine, made.tenant.id) as conn:
                for team, email in mine:
                    person = await create_user(conn, email)
                    _, token = await issue_token(
                        conn,
                        user_id=person.id,
                        name="loadtest",
                        expires_at=clock() + timedelta(days=30),
                        created_by="system:loadtest",
                    )
                    population.users.append(SimUser(slug, team, email, token))
            csv = "email,role,team\n" + "".join(f"{e},developer,{tm}\n" for tm, e in mine)
            started = time.perf_counter()
            imported = await http.post(
                f"{API_PREFIX}/rbac/import",
                json={"csv": csv, "dry_run": False},
                headers=admin,
                timeout=600,
            )
            population.import_seconds.append(round(time.perf_counter() - started, 3))
            if imported.status_code != 200:
                raise RuntimeError(f"role import for {slug} failed: {imported.text[:500]}")
    return population


def _people(slug: str, t: int, tenants: int, teams: list[str], users: int) -> list:
    """This tenant's share of `users` as (team, email), round-robin over its teams."""
    share = users // tenants + (1 if t <= users % tenants else 0)
    return [(teams[n % len(teams)], f"dev{n + 1:04d}@{slug}.example.test") for n in range(share)]


def registration(population: Population, user: SimUser, run_id: str) -> dict:
    """What `agentic run` registers (`agentic_cli.org_preflight.registration`) for a
    feature-delivery run of `user` in their team's repository."""
    return {
        "run_id": run_id,
        "team": user.team,
        "repo": f"git.example.test/{user.team}/service",
        "pipeline": PIPELINE,
        "pipeline_file": PIPELINE_FILE,
        "pipeline_sha256": population.pipeline_sha256,
        "pipeline_version": "1.0.0",
        "overlay_file": None,
        "overlay_sha256": None,
        "policy_version": population.policy_version[user.tenant],
        "agents": [f"{agent}@1.0.0" for agent in AGENTS],
        "models": list(MODELS),
        "backends": ["native"],
        "dry_run": False,
    }


def remove_tree(path: Path) -> None:
    """Remove a work folder; git makes pack files read-only (Windows needs them writable)."""

    def writable(func, target, _error):
        os.chmod(target, 0o700)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=writable)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _git(repo: Path, *args: str) -> None:
    done = subprocess.run(
        [*_GIT, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
