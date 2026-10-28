"""The agent-CLI backends as the CLI builds them (M2-60, decision 89): each one locked down
with the guard's shell policy and deny lists, its srt process wrap (TokenLean and the
registries only), the run's identity, and the post-step protected-path check; and what
preflight says when one cannot run here. Real git and Zyroflo trees; no agent CLI runs."""

import asyncio
import json
import re
import sys
import threading
import tomllib
from decimal import Decimal
from pathlib import Path

import pytest
from test_wiring import FIXTURES, RUN, SANDBOXED, UNSANDBOXED, step
from test_wiring import agent as agent  # noqa: F401 - a fixture
from test_wiring import repo as repo  # noqa: F401 - a fixture

from agentic.agents.models import GatewaySettings
from agentic.agents.runner import AgentOutcome
from agentic.backends.acp import ShellRules as AcpShellRules
from agentic.engine.preflight import PreflightReport
from agentic.policy import SandboxPolicy, load_policy
from agentic.sandbox.readiness import SandboxDecision
from agentic.transport import CallIdentity
from agentic_cli import agent_backends
from agentic_cli.agent_backends import (
    AGENT_CLIS,
    CONFIGURED_BACKENDS,
    ProtectedWriteError,
    ProtectedWrites,
    build,
    missing,
    problems,
)
from agentic_cli.errors import CliError
from agentic_cli.wiring import StepWiring
from sdlc_commons.guards import FRAMEWORK_DENY_SHELL, Guard

KEY = "tl-" + "unit-" + "key"  # built from pieces: not a real key shape
GATEWAY = GatewaySettings(base_url="https://tokenlean.acme.example", api_key=KEY)
IDENTITY = CallIdentity(
    team="payments",
    user="dev@acme.example",
    repo="repo",
    pipeline="deliver",
    run_id=RUN,
    step="develop",
    agent="developer@1.2.0",
    policy_version="v1",
)


def policy(**sandbox):
    loaded = load_policy(FIXTURES / "policy" / "policy")
    return loaded.model_copy(update={"sandbox": SandboxPolicy(**sandbox)})


def wiring_for(repo, tmp_path, decision=UNSANDBOXED, **sandbox) -> StepWiring:
    w = StepWiring(
        repo, SandboxPolicy(**sandbox), decision, repo / ".agentic" / "local" / "state", tmp_path
    )
    w.gateway_url = GATEWAY.base_url
    return w


def backends(repo, tmp_path, decision=UNSANDBOXED, settings=None, **sandbox):
    w = wiring_for(repo, tmp_path, decision, **sandbox)
    built = build(
        repo=repo,
        policy=policy(**sandbox),
        settings=settings or {},
        gateway=GATEWAY,
        wiring=w,
        identity_for=lambda s: IDENTITY,
        fallback_estimate_usd=Decimal("0.05"),
    )
    return built, w


def srt_settings(prefix: list[str]) -> dict:
    return json.loads(Path(prefix[prefix.index("--settings") + 1]).read_text("utf-8"))


# ── what is built ─────────────────────────────────────────────────────────────


def test_every_agent_cli_backend_is_configured():
    assert CONFIGURED_BACKENDS == ("native", "claude-code", "codex", "opencode")
    assert AGENT_CLIS == CONFIGURED_BACKENDS[1:]


def test_each_backend_is_built_behind_the_protected_path_check(repo, tmp_path):
    built, _ = backends(repo, tmp_path)
    assert set(built) == set(AGENT_CLIS)
    for name, backend in built.items():
        assert backend.name == name and backend.inner.name == name


def test_claude_code_gets_the_guards_permissions_and_its_own_config_root(repo, tmp_path, agent):
    built, w = backends(repo, tmp_path, pass_env=["SSH_AUTH_SOCK"])
    inner = built["claude-code"].inner
    assert (
        inner.permissions_for(step(agent)) == Guard.for_agent(repo, RUN, agent).claude_permissions()
    )
    assert inner.settings.gateway_url == GATEWAY.base_url and inner.settings.api_key == KEY
    assert inner.settings.config_root == repo / ".agentic" / "local" / "state" / "claude-code"
    assert inner.settings.pass_env == ("SSH_AUTH_SOCK",)
    assert inner.identity_for(step(agent)) == IDENTITY
    assert inner.wrap_for(step(agent)) is None  # the regime runs on guards: no srt here


def test_with_srt_claude_code_is_wrapped_writing_only_its_config_dir(repo, tmp_path, agent):
    built, w = backends(repo, tmp_path, SANDBOXED, allowed_domains=["pypi.org"])
    prefix = built["claude-code"].inner.wrap_for(step(agent))
    settings = srt_settings(prefix)
    root = repo / ".agentic" / "local" / "state" / "claude-code"
    config = root / RUN / "develop-1" / agent.definition.name
    assert config.as_posix() in settings["filesystem"]["allowWrite"]
    assert settings["network"]["allowedDomains"] == ["tokenlean.acme.example", "pypi.org"]


def test_codex_gets_the_guards_rules_with_the_framework_floor(repo, tmp_path, agent):
    built, _ = backends(repo, tmp_path, SANDBOXED)
    rules = built["codex"].inner.rules_for(step(agent))
    assert "git config" in rules.deny and set(FRAMEWORK_DENY_SHELL) <= set(rules.deny)
    assert set(agent.deny_shell) <= set(rules.deny)
    assert "echo" in rules.allow and "*" not in rules.allow


def test_codex_hook_spec_is_written_outside_the_tree_and_its_home(repo, tmp_path, agent):
    built, w = backends(repo, tmp_path, SANDBOXED)
    tree = w.tools_for(step(agent)).tree
    hook = built["codex"].inner.hook_for(step(agent), tree)
    assert hook.argv[:3] == (sys.executable, "-I", "-m")
    spec = Path(hook.argv[-1])
    assert spec.is_file() and not spec.is_relative_to(tree)
    assert spec.is_relative_to(repo / ".agentic" / "local" / "state" / RUN / "hooks")
    written = json.loads(spec.read_text(encoding="utf-8"))
    assert written["tree"] == str(tree) and written["allow_shell"] == list(agent.shell)


def test_a_missing_hook_interpreter_is_refused_at_lock_down(repo, tmp_path, agent, monkeypatch):
    # DF-429: Codex fails open on a hook that cannot start - so it must never be wired so
    import sdlc_commons.codex_hook as codex_hook

    built, w = backends(repo, tmp_path, SANDBOXED)
    monkeypatch.setattr(codex_hook.sys, "executable", str(tmp_path / "gone" / "python"))
    with pytest.raises(RuntimeError, match="guard hook"):
        built["codex"].inner.hook_for(step(agent), w.tools_for(step(agent)).tree)


def test_codex_wrap_denies_its_own_config_rules_catalog_and_hooks(repo, tmp_path, agent):
    built, _ = backends(repo, tmp_path, SANDBOXED)  # DF-425
    settings = srt_settings(built["codex"].inner.wrap_for(step(agent)))
    home = (repo / ".agentic" / "local" / "state" / "codex" / RUN / "develop-1").resolve()
    home = home / agent.definition.name
    denied = settings["filesystem"]["denyWrite"]
    assert home.as_posix() in settings["filesystem"]["allowWrite"]
    for name in ("config.toml", "rules", "models.json", "schema.json"):
        assert (home / name).as_posix() in denied, name
    hooks = repo / ".agentic" / "local" / "state" / RUN / "hooks"
    assert hooks.as_posix() in denied


def test_each_backend_wrap_allows_only_the_gateway_and_registries(repo, tmp_path, agent):
    built, _ = backends(repo, tmp_path, SANDBOXED, allowed_domains=["pypi.org"])
    for name in AGENT_CLIS:
        prefix = built[name].inner.wrap_for(step(agent))
        assert "NO_PROXY=" in prefix, name  # every request through srt's proxy
        hosts = srt_settings(prefix)["network"]["allowedDomains"]
        assert hosts == ["tokenlean.acme.example", "pypi.org"], name


def test_opencode_gets_the_guards_shell_rules_and_a_home_under_state(repo, tmp_path, agent):
    settings = {"agent_clis": {"opencode": str(tmp_path / "bin" / "opencode")}}
    built, _ = backends(repo, tmp_path, settings=settings)
    inner = built["opencode"].inner
    guard = Guard.for_agent(repo, RUN, agent)
    assert inner.rules_for(step(agent)) == AcpShellRules(**guard.shell_rules())
    assert inner.settings.home_root == repo / ".agentic" / "local" / "state" / "opencode"
    assert inner.settings.command[0] == str(tmp_path / "bin" / "opencode")
    assert inner.settings.command[1:3] == ("acp", "--pure")
    # OpenCode has no sandbox of its own: never run unwrapped (decision 92)
    with pytest.raises(ValueError, match="srt"):
        inner.wrap_for(step(agent))


def test_an_agent_cli_setting_must_name_a_program(repo, tmp_path):
    with pytest.raises(CliError, match="agent_clis"):
        backends(repo, tmp_path, settings={"agent_clis": {"codex": 42}})
    with pytest.raises(CliError, match="agent_clis"):
        backends(repo, tmp_path, settings={"agent_clis": ["codex"]})


# ── the protected-path check ──────────────────────────────────────────────────


class _Runner:
    def __init__(self, status="completed") -> None:
        self.status = status

    async def run(self, task):
        return AgentOutcome(status=self.status, output="x", stopped=None, usage=None, costs=None)

    async def resume(self, *args):
        return await self.run(None)


def test_a_step_that_wrote_a_protected_path_fails_with_the_paths_named():
    found = ["x/.github/workflows/ci.yml: matches the deny rule (framework floor)"]
    runner = ProtectedWrites(_Runner(), lambda: found, "codex")
    with pytest.raises(ProtectedWriteError, match="ci.yml") as raised:
        asyncio.run(runner.run(None))
    assert "codex" in str(raised.value) and "not kept" in str(raised.value)
    with pytest.raises(ProtectedWriteError):
        asyncio.run(runner.resume(Path("s"), {}, "d"))


def test_a_clean_or_stopped_step_passes_through():
    clean = ProtectedWrites(_Runner(), lambda: [], "codex")
    assert asyncio.run(clean.run(None)).status == "completed"
    stopped = ProtectedWrites(_Runner("stopped"), lambda: ["anything"], "codex")
    assert asyncio.run(stopped.run(None)).status == "stopped"  # never checkpointed anyway


def test_the_check_runs_off_the_event_loop():  # review F4: the watchdog keeps polling
    threads = []

    def check() -> list[str]:
        threads.append(threading.get_ident())
        return []

    asyncio.run(ProtectedWrites(_Runner(), check, "codex").run(None))
    assert threads and threads[0] != threading.get_ident()


def test_the_built_backend_checks_the_steps_trees(repo, tmp_path, agent):
    built, w = backends(repo, tmp_path)
    tree = w.tools_for(step(agent)).tree
    w.trees_for(RUN).begin(fresh=True)
    (tree / ".github" / "workflows").mkdir(parents=True)
    (tree / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    runner = built["opencode"].wrap(_Runner(), step(agent))
    with pytest.raises(ProtectedWriteError, match="ci.yml"):
        asyncio.run(runner.run(None))


STEERING = (
    ".opencode/plugin/x.js",
    "opencode.json",
    "sub/opencode.jsonc",
    ".codex/config.toml",
    ".agents/skills/x/SKILL.md",
    "AGENTS.override.md",
    ".mcp.json",
    "CLAUDE.local.md",
)


@pytest.mark.parametrize("backend", AGENT_CLIS)
def test_no_agent_cli_may_plant_an_agent_clis_steering_files(backend, repo, tmp_path, agent):
    # review F5: these steer (or run code in) the next agent CLI a developer opens on the
    # branch; the framework floor names Claude Code's own, not every agent CLI's
    built, w = backends(repo, tmp_path)
    tree = w.tools_for(step(agent)).tree
    w.trees_for(RUN).begin(fresh=True)
    for name in STEERING:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text("{}\n", encoding="utf-8")
    runner = built[backend].wrap(_Runner(), step(agent))
    with pytest.raises(ProtectedWriteError) as raised:
        asyncio.run(runner.run(None))
    refused = " ".join(raised.value.refused)
    assert all(Path(name).name in refused for name in STEERING), refused


# ── what this machine lacks ───────────────────────────────────────────────────


def test_a_missing_extra_says_exactly_what_to_install(monkeypatch):
    real = agent_backends.importlib.util.find_spec
    monkeypatch.setattr(
        agent_backends.importlib.util,
        "find_spec",
        lambda name: None if name == "claude_agent_sdk" else real(name),
    )
    why = missing("claude-code", {})
    assert "agentic-core[claude-code]" in why and "uv tool install" in why


CORE_EXTRAS = tomllib.loads(
    (Path(__file__).resolve().parents[2] / "core" / "pyproject.toml").read_text("utf-8")
)["project"]["optional-dependencies"]


@pytest.mark.parametrize("backend", AGENT_CLIS)
def test_each_missing_extras_hint_names_an_extra_core_has(backend, monkeypatch):  # review F3
    monkeypatch.setattr(agent_backends, "native_windows", lambda: False)
    monkeypatch.setattr(agent_backends.importlib.util, "find_spec", lambda name: None)
    why = missing(backend, {})
    named = re.findall(r"agentic-core\[([^\]]+)\]", why)
    assert named and all(extra in CORE_EXTRAS for extra in named), why
    assert f"the {named[0]} extra" in why


def test_codex_on_native_windows_points_to_wsl2(monkeypatch):
    monkeypatch.setattr(agent_backends, "native_windows", lambda: True)
    assert "WSL2" in missing("codex", {})


def test_codex_of_another_version_names_the_pinned_one(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_backends, "native_windows", lambda: False)
    fake = tmp_path / "codex_version.py"
    fake.write_text("print('codex-cli 0.1.0')\n", encoding="utf-8")
    why = missing("codex", {"agent_clis": {"codex": [sys.executable, str(fake)]}})
    assert "0.160.1" in why and "0.1.0" in why
    fake.write_text("print('codex-cli 0.160.1')\n", encoding="utf-8")
    assert missing("codex", {"agent_clis": {"codex": [sys.executable, str(fake)]}}) is None


def test_a_missing_opencode_names_the_release_to_install(tmp_path):
    why = missing("opencode", {"agent_clis": {"opencode": str(tmp_path / "no" / "opencode")}})
    assert "opencode-ai@1.18.35" in why


def test_native_never_lacks_anything():
    assert missing("native", {}) is None


# ── preflight ─────────────────────────────────────────────────────────────────


def report_for(resolved_agents) -> PreflightReport:
    return PreflightReport(pipeline="deliver", problems=[], resolved=resolved_agents)


@pytest.mark.parametrize("backend", ["codex", "opencode"])  # decisions 22 and 92
def test_an_agent_cli_without_its_own_sandbox_is_refused_without_srt(
    agent, repo, monkeypatch, backend
):
    monkeypatch.setattr(agent_backends, "missing", lambda backend, settings: None)
    codex = agent.model_copy(update={"backend": backend})
    found = problems(report_for({"dev": codex}), UNSANDBOXED, repo=repo, settings={})
    assert [(p.check, p.severity) for p in found] == [("backend", "error")]
    assert "srt" in found[0].detail and "dev" in found[0].detail


def test_a_backend_that_cannot_run_here_is_one_error_with_the_install_hint(
    agent, repo, monkeypatch
):
    monkeypatch.setattr(agent_backends, "missing", lambda b, s: f"install {b} like so")
    agents = {
        "dev": agent.model_copy(update={"backend": "claude-code"}),
        "rev": agent.model_copy(update={"backend": "claude-code"}),
        "ba": agent,  # native
    }
    found = problems(report_for(agents), SANDBOXED, repo=repo, settings={})
    assert len(found) == 1 and found[0].severity == "error"
    assert "dev, rev" in found[0].detail and "install claude-code like so" in found[0].detail


def test_a_sandbox_wrap_on_native_windows_is_refused(agent, repo, monkeypatch):  # DF-81
    monkeypatch.setattr(agent_backends, "missing", lambda backend, settings: None)
    monkeypatch.setattr(agent_backends, "native_windows", lambda: True)
    opencode = agent.model_copy(update={"backend": "opencode"})
    found = problems(report_for({"dev": opencode}), SANDBOXED, repo=repo, settings={})
    assert found and "WSL2" in found[0].detail


def test_opencode_plugins_in_the_repo_are_refused_before_any_spend(agent, repo, monkeypatch):
    monkeypatch.setattr(agent_backends, "missing", lambda backend, settings: None)
    (repo / ".opencode" / "plugin").mkdir(parents=True)
    (repo / ".opencode" / "plugin" / "x.js").write_text("export default {}", encoding="utf-8")
    monkeypatch.setattr(agent_backends, "native_windows", lambda: False)
    opencode = agent.model_copy(update={"backend": "opencode"})
    found = problems(report_for({"dev": opencode}), SANDBOXED, repo=repo, settings={})
    assert any("x.js" in p.detail and p.severity == "error" for p in found)


def test_native_steps_add_nothing(agent, repo):
    assert problems(report_for({"ba": agent}), UNSANDBOXED, repo=repo, settings={}) == []


def test_a_refused_sandbox_is_left_to_the_sandbox_check(agent, repo, monkeypatch):
    monkeypatch.setattr(agent_backends, "missing", lambda backend, settings: None)
    refused = SandboxDecision(False, "none", "team runs need a sandbox", None)
    codex = agent.model_copy(update={"backend": "codex"})
    assert problems(report_for({"dev": codex}), refused, repo=repo, settings={}) == []


def test_the_codex_hook_is_honoured_in_the_capability_matrix():
    assert agent_backends.HONOURED == {"codex": frozenset({"shell_default_deny"})}


def test_unpriced_calls_cost_what_the_runtime_says(repo, tmp_path):
    built, _ = backends(repo, tmp_path)
    assert {b.inner.fallback_estimate_usd for b in built.values()} == {Decimal("0.05")}
