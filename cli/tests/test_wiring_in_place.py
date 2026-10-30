"""The step wiring on an in-place component (M2-61): org policy's `commit_override` and the
repo config's `rollback_commands` reach the run's checkpoints, and a code step works in the
component's own checkout and is committed by its own tool."""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from agentic.definitions import load_agent
from agentic.engine.engine import StepContext
from agentic.policy import (
    CommitOverridePolicy,
    ResolveContext,
    SandboxPolicy,
    load_policy,
    resolve_agent,
)
from agentic.sandbox.readiness import SandboxDecision
from agentic.transport import BudgetLedger
from agentic_cli.wiring import StepWiring
from sdlc_commons.worktrees import WorktreeError

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "core" / "tests" / "fixtures"
_SPEC = importlib.util.spec_from_file_location(
    "commons_in_place_support", ROOT / "commons" / "tests" / "in_place_support.py"
)
support = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(support)
UNSANDBOXED = SandboxDecision(False, "none", None, "policy guards only")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("VCS_TOOL_LOG", str(tmp_path / "tool.log"))
    repo = support.make_in_place_repo(tmp_path / "repo")
    support.open_ledger(repo)
    return repo


@pytest.fixture
def agent():
    policy = load_policy(FIXTURES / "policy" / "policy")
    resolved, violations = resolve_agent(
        load_agent(FIXTURES / "agents" / "developer.md"), policy, ResolveContext(team="payments")
    )
    assert resolved is not None, violations
    return resolved.model_copy(update={"tools": ["read_files", "edit_files"]})


def step(agent) -> StepContext:
    budget = BudgetLedger(Decimal("5")).run.open("develop", "step", Decimal("1"))
    return StepContext(support.RUN, "develop", 1, agent, budget, None)


def wiring(repo: Path, tmp_path: Path, allow=support.ALLOW) -> StepWiring:
    return StepWiring(
        repo,
        SandboxPolicy(),
        UNSANDBOXED,
        tmp_path / "state",
        tmp_path / "home",
        commit_override=CommitOverridePolicy(allow=list(allow)),
        rollback_commands=dict(support.ROLLBACKS),
    )


def test_a_code_step_on_an_in_place_component_is_committed_by_its_tool(repo, tmp_path, agent):
    w = wiring(repo, tmp_path)
    context = w.tools_for(step(agent))
    assert context.tree == repo
    base = w.trees_for(support.RUN).begin(fresh=True)
    (context.tree / "src").mkdir()
    (context.tree / "src" / "new.py").write_text("y = 1\n", encoding="utf-8")
    assert context.guard.writes([context.tree / "src" / "new.py"]) == []
    assert context.guard.writes([context.tree / ".sdlc" / "profile.yml"]) != []
    head = w.trees_for(support.RUN).finish("agentic: develop #1")
    assert head != base
    assert support.git(repo, "log", "-1", "--format=%s") == "agentic: develop #1"
    log = (tmp_path / "tool.log").read_text(encoding="utf-8").split()
    assert log == ["commit"]


def test_without_org_allow_the_step_is_refused_naming_the_setting(repo, tmp_path, agent):
    with pytest.raises(WorktreeError, match=r"commit_override\.allow"):
        wiring(repo, tmp_path, allow=()).tools_for(step(agent))
