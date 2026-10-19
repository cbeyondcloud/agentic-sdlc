"""Tests for the step wiring: each agent step's tools get the run's tree, the agent's guard
and a sandbox (M1-30, DF-46; D38). Real git, real Zyroflo trees."""

import asyncio
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agentic.agents.tools import LocalProcessSandbox, build_tools
from agentic.definitions import load_agent
from agentic.engine.engine import StepContext
from agentic.policy import ResolveContext, SandboxPolicy, load_policy, resolve_agent
from agentic.sandbox import SrtSandbox
from agentic.sandbox.readiness import SandboxDecision
from agentic.transport import BudgetLedger
from agentic_cli.wiring import StepWiring
from sdlc_commons.ledger import RunIdentity, RunLedger

FIXTURES = Path(__file__).resolve().parents[2] / "core" / "tests" / "fixtures"
RUN = "r-wire-1"
GIT = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]
UNSANDBOXED = SandboxDecision(False, "none", None, "policy guards only")
SANDBOXED = SandboxDecision(True, "srt 0.0.78", None, None)


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    (path / "src").mkdir(parents=True)
    (path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    RunLedger(path, RUN).open(
        identity=RunIdentity(team="payments", policy_version="git:" + "a" * 40),
        kind="feature",
        project="demo",
        owner="platform",
        criticality="low",
    )
    return path


@pytest.fixture
def agent():
    policy = load_policy(FIXTURES / "policy" / "policy")
    resolved, violations = resolve_agent(
        load_agent(FIXTURES / "agents" / "developer.md"), policy, ResolveContext(team="payments")
    )
    assert resolved is not None, violations
    return resolved.model_copy(
        update={
            "tools": ["read_files", "edit_files", "run_shell"],
            "shell": ["echo", "git status"],
            "deny_paths": ["**/*.pem"],
        }
    )


def step(agent) -> StepContext:
    budget = BudgetLedger(Decimal("5")).run.open("develop", "step", Decimal("1"))
    return StepContext(RUN, "develop", 1, agent, budget, None)


def wiring(repo: Path, tmp_path: Path, decision=UNSANDBOXED, **policy) -> StepWiring:
    return StepWiring(
        repo, SandboxPolicy(**policy), decision, tmp_path / "state", tmp_path / "home"
    )


def call(tools, tool_name: str, **args) -> str:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart(str(returns[-1].content))])
        return ModelResponse(parts=[ToolCallPart(tool_name, args)])

    return asyncio.run(Agent(FunctionModel(model), tools=tools).run("go")).output


def test_tools_write_inside_the_run_tree_and_the_step_commit_holds_it(repo, tmp_path, agent):
    w = wiring(repo, tmp_path)
    context = w.tools_for(step(agent))
    tools = build_tools(agent, context)

    assert "src/new.py" in call(tools, "write_file", path="src/new.py", text="y = 2\n")
    assert (context.tree / "src" / "new.py").is_file()
    commit = w.trees_for(RUN).finish("agentic: develop #1")
    assert git(context.tree, "show", "--name-only", "--format=", commit["repo"]) == "src/new.py"
    assert not (repo / "src" / "new.py").exists()  # the developer's checkout is untouched


def test_tools_refuse_a_write_outside_the_tree(repo, tmp_path, agent):
    tools = build_tools(agent, wiring(repo, tmp_path).tools_for(step(agent)))
    assert call(tools, "write_file", path="../../escape.py", text="x").startswith("REFUSED")


def test_shell_runs_in_the_tree_and_refuses_unlisted_programs(repo, tmp_path, agent):
    context = wiring(repo, tmp_path).tools_for(step(agent))
    assert isinstance(context.sandbox, LocalProcessSandbox) and context.allow_unisolated
    tools = build_tools(agent, context)

    assert call(tools, "run_shell", command="echo ok").startswith("exit 0")
    assert "not on the allow list" in call(tools, "run_shell", command="ls")


def test_a_refused_decision_cannot_build_tools(repo, tmp_path, agent):
    refused = SandboxDecision(False, "none", "the 'regulated' regime needs a sandbox", None)
    with pytest.raises(ValueError, match="regulated"):
        wiring(repo, tmp_path, refused).tools_for(step(agent))


def test_srt_rules_carry_policy_and_guard(repo, tmp_path, agent):
    w = wiring(
        repo,
        tmp_path,
        SANDBOXED,
        allowed_domains=["pypi.org"],
        allow_write=["~/.cache/uv"],
        deny_read=["~/secrets"],
    )
    context = w.tools_for(step(agent))
    assert isinstance(context.sandbox, SrtSandbox) and not context.allow_unisolated
    rules = context.sandbox.rules
    home = tmp_path / "home"
    assert rules.write == (context.tree, home / ".cache" / "uv")
    assert context.tree / ".env" in rules.deny_write  # Zyroflo's protected `.env`
    assert home / ".ssh" in rules.deny_read and home / "secrets" in rules.deny_read
    assert rules.allowed_domains == ("pypi.org",)
    assert context.sandbox.settings.parent == tmp_path / "state" / RUN / "sandbox" / "developer"


def test_unenforced_globs_are_reported(repo, tmp_path, agent):
    assert "**/*.pem" in wiring(repo, tmp_path).unenforced(agent, RUN)
