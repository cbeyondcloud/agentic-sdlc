"""scripts/bootstrap.py (M3-05): the day-0 steps against fakes (kubectl, the agentic CLI, an
HTTP control plane and index), except the policy repo, which is really built and must pass
the real policy-repo gate. No secret reaches stdout; re-runs skip what exists."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap  # noqa: E402

from agentic.skills.gate import check_policy_repo

TOKEN = "acp_" + "x" * 40  # a stand-in first-admin token


def _args(**values):
    parser = bootstrap.build_parser()
    argv = [values.pop("step")]
    for key, value in values.items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif isinstance(value, list):
            for item in value:
                argv += [flag, item]
        else:
            argv += [flag, value]
    return parser.parse_args(argv)


# ── 1. the policy repo ────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def test_the_policy_repo_is_the_template_with_the_orgs_names_and_passes_the_gate(tmp_path):
    out = tmp_path / "policy"
    args = _args(
        step="policy-repo",
        out=str(out),
        repo_pattern=["git.acme.example/acme/*"],
        admins=["@acme/platform-admins"],
        team=["checkout=@acme/checkout-leads", "search=@acme/search-leads"],
        model=["balanced=acme-gw/balanced-v1", "fast=acme-gw/fast-v1"],
        cli_version="1.4.0",
    )
    bootstrap.build_policy_repo(args)
    assert check_policy_repo(out) == []
    codeowners = (out / "CODEOWNERS").read_text(encoding="utf-8")
    assert "@your-org" not in codeowners and "payments" not in codeowners
    assert "/policy/teams/checkout.yaml @acme/checkout-leads @acme/platform-admins" in codeowners
    assert not (out / "policy" / "teams" / "payments.yaml").exists()
    assert (out / "policy" / "teams" / "search.yaml").is_file()
    assert not list((out / "teams" / "search" / "pipelines" / "overlays").glob("*.yaml"))
    org = (out / "policy" / "org.yaml").read_text(encoding="utf-8")
    assert 'allowed_repo_patterns: ["git.acme.example/acme/*"]' in org
    tiers = (out / "policy" / "tiers.yaml").read_text(encoding="utf-8")
    assert "model: acme-gw/balanced-v1" in tiers
    assert "model: change-me/deep-reasoning-model" in tiers  # a tier not named stays
    workflow = (out / ".github" / "workflows" / "policy-check.yml").read_text(encoding="utf-8")
    assert 'AGENTIC_VERSION: "1.4.0"' in workflow
    assert (out / ".github" / "budget_guard.py").is_file()
    # the library is copied in, as the template's README asks
    assert {p.name for p in (out / "agents").glob("*.md")} >= {"developer.md"}
    assert (out / "skills" / "catalog.yaml").read_text(encoding="utf-8") == (
        bootstrap.LIBRARY / "skills" / "catalog.yaml"
    ).read_text(encoding="utf-8")
    assert set(_git(out, "branch", "--format=%(refname:short)").split()) == {
        "main",
        "staging",
        "dev",
    }
    assert _git(out, "status", "--porcelain") == ""


def test_the_policy_repo_step_refuses_a_used_folder_and_an_unknown_tier(tmp_path):
    used = tmp_path / "used"
    used.mkdir()
    (used / "keep.txt").write_text("x", encoding="utf-8")
    admins = ["@acme/admins"]
    with pytest.raises(bootstrap.BootstrapError, match="not empty"):
        bootstrap.build_policy_repo(_args(step="policy-repo", out=str(used), admins=admins))
    with pytest.raises(bootstrap.BootstrapError, match="no tier huge"):
        bootstrap.build_policy_repo(
            _args(step="policy-repo", out=str(tmp_path / "p"), model=["huge=x/y"], admins=admins)
        )
    # review: a failed build leaves nothing behind, so the step can be run again
    assert not (tmp_path / "p").exists()


def test_the_policy_repo_command_runs_the_cli_gate_and_skips_a_second_build(tmp_path, capsys):
    out = tmp_path / "policy"
    argv = ["policy-repo", "--out", str(out), "--model", "balanced=acme-gw/b"]
    argv += ["--admins", "@acme/agentic-org-admins"]
    assert bootstrap.main(argv) == 0
    printed = capsys.readouterr().out
    assert "passes the policy-repo gate" in printed and "next: push" in printed
    assert bootstrap.main([*argv, "--no-check"]) == 0
    assert "already a git repository" in capsys.readouterr().out


# ── 2. git credentials ────────────────────────────────────────────────────────


def test_git_credentials_name_the_token_file_never_the_token(tmp_path, capsys):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    argv = ["git-credentials", "--tenant", "acme", "--out", str(tmp_path / "git")]
    assert bootstrap.main([*argv, "--host", "git.acme.example", "--ca-file", str(ca)]) == 0
    text = (tmp_path / "git" / "acme.gitconfig").read_text(encoding="utf-8")
    assert '[credential "https://git.acme.example"]' in text
    assert "cat /etc/agentic/git/acme.token" in text
    assert "sslCAInfo = /etc/agentic/git/acme-ca.pem" in text
    assert (tmp_path / "git" / "acme-ca.pem").is_file()
    printed = capsys.readouterr().out
    assert "--from-file=acme.token=<the token file>" in printed
    assert bootstrap.main(["git-credentials", "--tenant", "Bad Slug", "--out", "x"]) == 3
    shell = ["--username", "x; rm -rf /"]  # review: the user name goes into a shell snippet
    assert bootstrap.main([*argv, *shell]) == 3


# ── 3. the tenant ─────────────────────────────────────────────────────────────


class FakeKubectl:
    """kubectl with an operator pod whose `python -m agentic_cp` answers like the real one."""

    def __init__(self, tenants=(), create_fails=False, rollout_fails=False):
        self.tenants, self.create_fails, self.calls = list(tenants), create_fails, []
        self.rollout_fails, self.replicas = rollout_fails, 0

    def __call__(self, cmd, **_):
        self.calls.append(cmd)
        args = cmd[cmd.index("-n") + 2 :]
        out, code = "", 0
        if args[0] == "scale":
            self.replicas = int(args[2].split("=")[1])
        elif args[0] == "rollout" and self.rollout_fails:
            return subprocess.CompletedProcess(cmd, 1, "", "error: timed out waiting")
        elif args[0] == "exec":
            assert self.replicas == 1, "exec into a scaled-down operator"
            argv = args[args.index("agentic_cp") + 1 :]
            if argv[:3] == ["admin", "tenant", "list"]:
                out = "".join(f"{t}\tactive\t{t}\t-@staging\n" for t in self.tenants)
            elif argv[:3] == ["admin", "tenant", "create"]:
                if self.create_fails:
                    # stdout as the real command prints it, the token included
                    printed = f"token (shown once - store it now):\n{TOKEN}\n"
                    return subprocess.CompletedProcess(cmd, 3, printed, "error: bad email")
                slug = argv[argv.index("--slug") + 1]
                self.tenants.append(slug)
                out = (
                    f"tenant {slug} created (id 1)\nfirst org admin: a@acme.example\n"
                    f"token (shown once - store it now):\n{TOKEN}\n"
                )
            elif argv[:2] == ["admin", "issue-token"]:
                out = f"token issued\ntoken (shown once - store it now):\n{TOKEN}-2\n"
            elif argv[:3] == ["admin", "tenant", "set-policy-repo"]:
                out = "tenant acme: policy repo set (branch staging); the worker syncs it next\n"
        return subprocess.CompletedProcess(cmd, code, out, "")


def _tenant_args(tmp_path, **extra):
    values = dict(
        step="tenant",
        slug="acme",
        name="Acme",
        admin_email="a@acme.example",
        token_file=str(tmp_path / "admin.token"),
        policy_repo="https://git.acme.example/acme/policy.git",
    )
    return _args(**{**values, **extra})


def test_the_tenant_step_keeps_the_token_off_the_terminal(tmp_path, capsys):
    kube = FakeKubectl()
    assert bootstrap.cmd_tenant(_tenant_args(tmp_path), runner=kube) == 0
    printed = capsys.readouterr().out
    assert TOKEN not in printed and "written to the token file" in printed
    assert (tmp_path / "admin.token").read_text(encoding="utf-8").strip() == TOKEN
    assert kube.replicas == 0  # scaled back down
    assert any("deploy/agentic-agentic-platform-operator" in c for c in kube.calls)
    assert any("set-policy-repo" in c for c in kube.calls)
    # again: the tenant exists, nothing is created and no token file is needed
    assert bootstrap.cmd_tenant(_tenant_args(tmp_path), runner=kube) == 0
    assert "exists: not created again" in capsys.readouterr().out
    assert sum("create" in c for c in kube.calls) == 1
    # a lost token: a recovery token for the same admin, into a new file
    recovered = tmp_path / "recovered.token"
    args = _tenant_args(tmp_path, token_file=str(recovered), recover_token=True)
    assert bootstrap.cmd_tenant(args, runner=kube) == 0
    assert recovered.read_text(encoding="utf-8").strip() == f"{TOKEN}-2"
    assert f"{TOKEN}-2" not in capsys.readouterr().out


def test_the_operator_is_scaled_down_even_when_a_step_fails(tmp_path, capsys):
    kube = FakeKubectl(create_fails=True)
    with pytest.raises(bootstrap.BootstrapError, match="bad email") as failed:
        bootstrap.cmd_tenant(_tenant_args(tmp_path), runner=kube)
    assert kube.replicas == 0 and not (tmp_path / "admin.token").exists()
    assert TOKEN not in str(failed.value)  # review: an error never quotes stdout
    # review: a rollout that times out still ends scaled down
    kube = FakeKubectl(rollout_fails=True)
    with pytest.raises(bootstrap.BootstrapError, match="timed out"):
        bootstrap.cmd_tenant(_tenant_args(tmp_path), runner=kube)
    assert kube.replicas == 0


def test_an_existing_token_file_is_never_overwritten(tmp_path):
    (tmp_path / "admin.token").write_text("old\n", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="exists"):
        bootstrap.cmd_tenant(_tenant_args(tmp_path), runner=FakeKubectl())
    assert (tmp_path / "admin.token").read_text(encoding="utf-8") == "old\n"


def test_the_operator_name_follows_the_charts_fullname():
    assert bootstrap.operator_name("agentic") == "agentic-agentic-platform-operator"
    assert bootstrap.operator_name("agentic-platform") == "agentic-platform-operator"


# ── 4. people ─────────────────────────────────────────────────────────────────


def test_people_assigns_through_the_cli_with_the_token_in_a_variable(tmp_path, capsys):
    token_file = tmp_path / "admin.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    seen = []

    def agentic(cmd, env=None, **_):
        seen.append((cmd, env))
        email, role = cmd[cmd.index("assign") + 1 : cmd.index("assign") + 3]
        home = Path(env["AGENTIC_HOME"])
        assert "org: acme" in (home / "config.yaml").read_text(encoding="utf-8")
        if email.startswith("known"):
            reason = "error: that assignment already exists"
            return subprocess.CompletedProcess(cmd, 2, "", reason)
        if email.startswith("expired"):  # review: other text with "already" is a failure
            return subprocess.CompletedProcess(cmd, 2, "", "error: session already expired")
        if email.startswith("bad"):
            return subprocess.CompletedProcess(cmd, 2, "", "error: a team lead may not assign")
        note = "invite queued, not mailed: no mail relay" if email.startswith("q") else None
        made = {"email": email, "role": role, "invited": True, "invite_note": note}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(made), "")

    args = _args(
        step="people",
        api="http://cp.test",
        org="acme",
        token_file=str(token_file),
        assign=["lead@acme.example=team-lead@checkout", "q@acme.example=developer@checkout"],
        agentic="agentic",
    )
    assert bootstrap.cmd_people(args, runner=agentic) == 0
    printed = capsys.readouterr().out
    assert "lead@acme.example holds team-lead in team checkout: invited" in printed
    assert "no mail relay" in printed and TOKEN not in printed
    cmd, env = seen[0]
    assert cmd[-2:] == ["--team", "checkout"] and env["AGENTIC_CONTROL_PLANE_TOKEN"] == TOKEN
    assert TOKEN not in " ".join(cmd)
    args.assign = ["known@acme.example=developer", "bad@acme.example=org-admin"]
    assert bootstrap.cmd_people(args, runner=agentic) == 1
    printed = capsys.readouterr().out
    assert "already holds" in printed and "refused" in printed
    args.assign = ["expired@acme.example=developer"]
    assert bootstrap.cmd_people(args, runner=agentic) == 1


# ── 5. verify, against a fake control plane and index ────────────────────────


class FakePlane:
    def __init__(self):
        self.policy = {"version": "git:" + "a" * 40, "branch": "staging"}
        self.mail_status = "sent"
        self.seen_tokens: list[str | None] = []

    def answer(self, path: str, token: str | None):
        self.seen_tokens.append(token)
        routes = {
            "/readyz": (200, {"status": "ready", "db_enforced": True}),
            "/v1/me": (
                200,
                {
                    "user": {"email": "a@acme.example"},
                    "tenant": {"slug": "acme"},
                    "assignments": [{"role": "org-admin", "scope": "org"}],
                },
            ),
            "/v1/policy/active": (200, self.policy) if self.policy else (404, {}),
            "/v1/policy/versions": (200, [{"problems": ["policy/tiers.yaml: bad"]}]),
            "/v1/rbac/assignments": (200, [{"role": "org-admin"}, {"role": "developer"}]),
            "/v1/notifications?limit=500": (
                200,
                [
                    {
                        "recipient": "dev@acme.example",
                        "kind": "invite",
                        "status": self.mail_status,
                        "last_error": None,
                    }
                ],
            ),
            "/simple/agentic-cli/": (200, '<a href="x">agentic_cli-1.4.0-py3-none-any.whl</a>'),
            "/simple/agentic-core/": (200, "<a>agentic_core-1.4.0-py3-none-any.whl</a>"),
        }
        return routes.get(path, (404, {}))


@pytest.fixture
def plane():
    fake = FakePlane()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            auth = self.headers.get("Authorization")
            status, body = fake.answer(self.path, auth)
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            sent = json.loads(self.rfile.read(length))
            ok = self.path == "/v1/invites/redeem" and sent["token"] == "aci_good"
            self.send_response(200 if ok else 404)
            self.end_headers()
            self.wfile.write(json.dumps({"control_plane_token": "acp_dev"}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


def _verify(plane, tmp_path, *extra: str) -> int:
    token_file = tmp_path / "admin.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    argv = ["verify", "--api", plane.url, "--token-file", str(token_file)]
    return bootstrap.main([*argv, *extra])


def test_verify_passes_a_bootstrapped_org(plane, tmp_path, capsys):
    extra = ["--mail-to", "dev@acme.example", "--index-url", plane.url + "/simple/"]
    assert _verify(plane, tmp_path, *extra, "--cli-version", "1.4.0") == 0
    printed = capsys.readouterr().out
    assert "0 failed" in printed and "MANUAL" in printed and TOKEN not in printed
    assert "agentic-cli 1.4.0 on the index" in printed
    assert plane.seen_tokens[0] is None  # /readyz without the token
    assert f"Bearer {TOKEN}" in plane.seen_tokens


def test_verify_names_what_is_missing(plane, tmp_path, capsys):
    plane.policy, plane.mail_status = None, "pending"
    index = ["--index-url", plane.url + "/simple/", "--cli-version", "9.9.9"]
    assert _verify(plane, tmp_path, "--mail-to", "dev@acme.example", *index) == 1
    printed = capsys.readouterr().out
    assert "policy/tiers.yaml: bad" in printed and "pending" in printed
    assert "agentic_cli-9.9.9-py3-none-any.whl not on" in printed


def test_verify_needs_a_version_with_an_index(plane, tmp_path):
    assert _verify(plane, tmp_path, "--index-url", plane.url + "/simple/") == 3


# ── 6. the smoke run ──────────────────────────────────────────────────────────


def test_the_smoke_run_redeems_the_invite_and_dry_runs_in_org_mode(plane, tmp_path, capsys):
    calls = []

    def runner(cmd, env=None, cwd=None, **kwargs):
        if cmd[0] == "git":
            return bootstrap.run(cmd, env=env, cwd=cwd, **kwargs)
        calls.append((cmd, env, cwd))
        assert env["AGENTIC_CONTROL_PLANE_TOKEN"] == "acp_dev"
        assert "team: checkout" in (Path(env["AGENTIC_HOME"]) / "config.yaml").read_text()
        out = json.dumps({"org": {"allowed": True}}) if "--dry-run" in cmd else "{}"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    args = _args(
        step="smoke",
        api=plane.url,
        org="acme",
        team="checkout",
        invite_link=f"{plane.url}/invite#aci_good",
        repo_remote="https://git.acme.example/acme/smoke.git",
        agentic="agentic",
    )
    assert bootstrap.cmd_smoke(args, runner=runner) == 0
    printed = capsys.readouterr().out
    assert "smoke run passed" in printed and "acp_dev" not in printed
    assert [c[0][2] for c in calls] == ["init", "run"]
    args.invite_link = f"{plane.url}/invite#aci_used"
    with pytest.raises(bootstrap.BootstrapError, match="used, expired or unknown"):
        bootstrap.cmd_smoke(args, runner=runner)


def test_token_from_hides_the_token_line():
    token, shown = bootstrap.token_from(f"created\ntoken (shown once - store it now):\n{TOKEN}\n")
    assert token == TOKEN and TOKEN not in shown
    assert bootstrap.token_from("no token here") == (None, "no token here")


def test_the_template_team_may_use_every_agent_types_tier():
    """Found by the kind proof's smoke run: the example team (copied for every team the
    bootstrap adds) left out `deep-reasoning`, the architect's tier, so feature-delivery's
    design step was refused for every team. Each type's default tier must stay allowed."""
    from agentic.yamlio import load_yaml

    policy = bootstrap.TEMPLATE / "policy"
    types = load_yaml((policy / "agent-types.yaml").read_text(encoding="utf-8"))[0]["types"]
    team = load_yaml((policy / "teams" / "payments.yaml").read_text(encoding="utf-8"))[0]
    used = {spec["tier"] for spec in types.values() if spec.get("tier")}
    assert used <= set(team["allowed_tiers"]), used - set(team["allowed_tiers"])


def test_byte_code_in_the_template_never_reaches_a_policy_repo(tmp_path, monkeypatch):
    """Found by the kind proof: a test that imported the budget guard left __pycache__ in
    the template, and the copy carried it into the policy repo's first commit."""
    import shutil

    template = tmp_path / "template"
    shutil.copytree(bootstrap.TEMPLATE, template)
    (template / ".github" / "__pycache__").mkdir()
    (template / ".github" / "__pycache__" / "budget_guard.cpython-312.pyc").write_bytes(b"\xcb")
    monkeypatch.setattr(bootstrap, "TEMPLATE", template)
    out = tmp_path / "policy"
    bootstrap.build_policy_repo(_args(step="policy-repo", out=str(out), admins=["@acme/a"]))
    assert not list(out.rglob("__pycache__")) and not list(out.rglob("*.pyc"))
