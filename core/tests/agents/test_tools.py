"""Tests for the agent tools: offered only when allowed, every path and command guarded (M1-16).

The tools run against a REAL run tree (a git worktree made by Zyroflo through the
commons adapter) with the REAL SP2 guard - not paths a test assembled by hand.
"""

import asyncio
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from agentic.agents.tools import (
    LocalProcessSandbox,
    ShellResult,
    ToolConfigError,
    ToolContext,
    build_tools,
)
from agentic.codeintel import Candidates
from sdlc_commons.guards import Guard
from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.worktrees import RunWorktrees

RUN = "r-tools-1"
GIT = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]


def git(where: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(where), *GIT, *args], capture_output=True, check=True)


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """(repo, the run's tree) - a real worktree with a few files committed."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def handler():\n    return 'old'\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Use the retry helper.\n", encoding="utf-8")
    (repo / "infra" / "prod").mkdir(parents=True)
    (repo / "infra" / "prod" / "main.tf").write_text("secret_infra = 1\n", encoding="utf-8")
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    RunLedger(repo, RUN).open(
        identity=RunIdentity(team="payments", policy_version="git:" + "a" * 40),
        kind="feature",
        project="demo",
        owner="platform",
        criticality="low",
    )
    (made,) = RunWorktrees(repo, RUN).create().values()
    (made.path / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    return repo, made.path


class FakeSandbox:
    isolated = True

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(self, command: str, cwd: Path, timeout_s: float) -> ShellResult:
        self.ran.append(command)
        return ShellResult(exit_code=0, stdout=f"ran in {cwd.name}", stderr="", timed_out=False)


class FakeCodeIntel:
    name = "fake"
    supports = frozenset({"dependents", "impact", "tests_for", "overview"})

    def dependents(self, path: str) -> Candidates:
        return Candidates((f"src/uses_{Path(path).stem}.py",), source=self.name)

    def impact(self, diff: str) -> Candidates:
        return Candidates(("src/app.py",), source=self.name)

    def tests_for(self, path: str) -> Candidates:
        return Candidates((f"tests/test_{Path(path).stem}.py",), source=self.name)

    def overview(self) -> str:
        return "2 files"


class FakeSkills:
    def get(self, name: str) -> str:
        return f"# {name}\nWrite the failing test first."


def context(tree, resolved, *, sandbox=None, allow_unisolated=False) -> ToolContext:
    repo, path = tree
    guard = Guard(repo, RUN, deny_shell=resolved.deny_shell, deny_paths=["infra/prod/**"])
    return ToolContext(
        tree=path,
        guard=guard,
        sandbox=sandbox or FakeSandbox(),
        codeintel=FakeCodeIntel(),
        skills=FakeSkills(),
        allow_unisolated=allow_unisolated,
    )


def agent_with(resolved, tools: list[str], skills: list[str] | None = None):
    return resolved.model_copy(update={"tools": tools, "skills": skills or resolved.skills})


def call(tools, tool_name: str, **args) -> str:
    """Run one tool call through a real Pydantic AI agent and return what the model saw."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart(str(returns[-1].content))])
        return ModelResponse(parts=[ToolCallPart(tool_name, args)])

    return Agent(FunctionModel(model), tools=tools).run_sync("go").output


# ── which tools exist ─────────────────────────────────────────────────────────


def test_only_allowed_tools_are_offered_to_the_model(tree, resolved):
    tools = build_tools(agent_with(resolved, ["read_files", "search"]), context(tree, resolved))
    seen: dict = {}

    def model(messages, info: AgentInfo) -> ModelResponse:
        seen["names"] = sorted(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("ok")])

    Agent(FunctionModel(model), tools=tools).run_sync("go")
    assert seen["names"] == ["list_dir", "read_file", "search"]


def test_a_call_to_a_tool_not_offered_is_not_run(tree, resolved):
    sandbox = FakeSandbox()
    tools = build_tools(
        agent_with(resolved, ["read_files"]), context(tree, resolved, sandbox=sandbox)
    )

    def model(messages, info) -> ModelResponse:
        if len(messages) > 1:
            return ModelResponse(parts=[TextPart("gave up")])
        return ModelResponse(parts=[ToolCallPart("run_shell", {"command": "ls"})])

    assert Agent(FunctionModel(model), tools=tools).run_sync("go").output == "gave up"
    assert sandbox.ran == []


def test_an_unknown_capability_is_a_configuration_error(tree, resolved):
    with pytest.raises(ToolConfigError, match="web_fetch"):
        build_tools(agent_with(resolved, ["web_fetch"]), context(tree, resolved))


# ── reading ───────────────────────────────────────────────────────────────────


def test_read_returns_a_file_in_the_tree(tree, resolved):
    tools = build_tools(agent_with(resolved, ["read_files"]), context(tree, resolved))

    assert "return 'old'" in call(tools, "read_file", path="src/app.py")
    assert "Use the retry helper." in call(tools, "read_file", path="AGENTS.md")  # readable


@pytest.mark.parametrize("path", ["../outside.txt", "infra/prod/main.tf", ".env"])
def test_read_refuses_outside_denied_and_secret_paths(tree, resolved, path):
    tools = build_tools(agent_with(resolved, ["read_files"]), context(tree, resolved))

    seen = call(tools, "read_file", path=path)
    assert seen.startswith("REFUSED")
    assert "secret_infra" not in seen and "TOKEN" not in seen


def test_read_refuses_an_absolute_path_outside(tree, resolved, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("private\n", encoding="utf-8")
    tools = build_tools(agent_with(resolved, ["read_files"]), context(tree, resolved))

    seen = call(tools, "read_file", path=str(outside))
    assert seen.startswith("REFUSED") and "private" not in seen


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_read_refuses_a_symlink_out_of_the_tree(tree, resolved, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private\n", encoding="utf-8")
    (tree[1] / "link.txt").symlink_to(secret)
    tools = build_tools(agent_with(resolved, ["read_files"]), context(tree, resolved))

    assert call(tools, "read_file", path="link.txt").startswith("REFUSED")


def test_list_dir_shows_the_tree(tree, resolved):
    tools = build_tools(agent_with(resolved, ["read_files"]), context(tree, resolved))

    assert "app.py" in call(tools, "list_dir", path="src")


# ── searching ─────────────────────────────────────────────────────────────────


def test_search_finds_matches_and_skips_denied_paths_and_binaries(tree, resolved):
    tools = build_tools(agent_with(resolved, ["search"]), context(tree, resolved))

    found = call(tools, "search", pattern="old|secret_infra|PNG|TOKEN")
    assert "src/app.py:2:" in found
    assert "secret_infra" not in found and "logo.png" not in found and "TOKEN" not in found


# ── editing ───────────────────────────────────────────────────────────────────


def test_edit_replaces_exactly_once(tree, resolved):
    tools = build_tools(agent_with(resolved, ["edit_files"]), context(tree, resolved))

    assert "edited" in call(tools, "edit_file", path="src/app.py", old="'old'", new="'new'")
    assert "'new'" in (tree[1] / "src" / "app.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("old", ["'missing'", "    "])  # absent, or not unique
def test_edit_refuses_text_that_is_missing_or_not_unique(tree, resolved, old):
    (tree[1] / "src" / "app.py").write_text("    a\n    b\n", encoding="utf-8")
    tools = build_tools(agent_with(resolved, ["edit_files"]), context(tree, resolved))

    assert call(tools, "edit_file", path="src/app.py", old=old, new="x").startswith("REFUSED")


@pytest.mark.parametrize(
    "path", ["../escape.py", "infra/prod/new.tf", ".github/workflows/ci.yml", "AGENTS.md"]
)
def test_a_write_refused_by_the_guard_writes_nothing(tree, resolved, path):
    tools = build_tools(agent_with(resolved, ["edit_files"]), context(tree, resolved))

    assert call(tools, "write_file", path=path, text="pwned").startswith("REFUSED")
    assert not (tree[1] / path).exists() or "pwned" not in (tree[1] / path).read_text(
        encoding="utf-8"
    )


def test_a_path_between_component_trees_is_outside_the_working_tree(tree, resolved):
    # the guard allows the whole run folder; the tools stay inside this tree
    tools = build_tools(agent_with(resolved, ["edit_files", "read_files"]), context(tree, resolved))

    assert call(tools, "write_file", path="../escape.py", text="x").startswith("REFUSED")
    assert not (tree[1].parent / "escape.py").exists()
    assert call(tools, "read_file", path="../worktrees.json").startswith("REFUSED")


def test_write_creates_a_file_in_the_tree(tree, resolved):
    tools = build_tools(agent_with(resolved, ["edit_files"]), context(tree, resolved))

    call(tools, "write_file", path="src/new.py", text="x = 1\n")
    assert (tree[1] / "src" / "new.py").read_text(encoding="utf-8") == "x = 1\n"


# ── the shell ─────────────────────────────────────────────────────────────────


def test_a_shell_command_refused_by_the_guard_never_runs(tree, resolved):
    sandbox = FakeSandbox()
    tools = build_tools(
        agent_with(resolved, ["run_shell"]), context(tree, resolved, sandbox=sandbox)
    )

    assert call(tools, "run_shell", command="git push origin main").startswith("REFUSED")
    assert sandbox.ran == []


def test_a_shell_command_runs_in_the_tree(tree, resolved):
    sandbox = FakeSandbox()
    tools = build_tools(
        agent_with(resolved, ["run_shell"]), context(tree, resolved, sandbox=sandbox)
    )

    assert f"ran in {tree[1].name}" in call(tools, "run_shell", command="pytest -q")
    assert sandbox.ran == ["pytest -q"]


def test_the_shell_needs_an_isolated_sandbox_unless_allowed(tree, resolved):
    unisolated = LocalProcessSandbox()
    with pytest.raises(ToolConfigError, match="isolated"):
        build_tools(
            agent_with(resolved, ["run_shell"]), context(tree, resolved, sandbox=unisolated)
        )

    build_tools(
        agent_with(resolved, ["run_shell"]),
        context(tree, resolved, sandbox=unisolated, allow_unisolated=True),
    )


def script(folder: Path, name: str, code: str) -> str:
    """A shell command running a small Python script (no nested quoting)."""
    path = folder / name
    path.write_text(code, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def test_the_local_sandbox_runs_and_times_out(tree, tmp_path):
    sandbox = LocalProcessSandbox()

    done = asyncio.run(sandbox.run(script(tmp_path, "a.py", "print(6 * 7)"), tree[1], 30))
    assert (done.exit_code, done.stdout.strip(), done.timed_out) == (0, "42", False)
    sleeper = script(tmp_path, "b.py", "import time\ntime.sleep(30)\n")
    slow = asyncio.run(sandbox.run(sleeper, tree[1], 1))
    assert slow.timed_out and slow.exit_code is None


def test_the_local_sandbox_caps_output_and_hides_secrets(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_TEST_API_TOKEN", "do-not-leak")
    sandbox = LocalProcessSandbox()

    big = asyncio.run(sandbox.run(script(tmp_path, "big.py", "print('x' * 200000)"), tree[1], 30))
    assert len(big.stdout) < 60_000 and "more" in big.stdout
    code = "import os\nprint(os.environ.get('AGENTIC_TEST_API_TOKEN', 'absent'))\n"
    env = asyncio.run(sandbox.run(script(tmp_path, "env.py", code), tree[1], 30))
    assert env.stdout.strip() == "absent"


def test_a_stopped_run_kills_its_shell_command(tree, resolved, tmp_path):  # review I2
    from agentic.agents.runner import AgentTask, PydanticAIRunner
    from agentic.transport import BudgetLedger

    late = tree[1] / "late.txt"
    code = f"import time\ntime.sleep(4)\nopen(r'{late}', 'w').write('x')\n"
    command = script(tmp_path, "late.py", code)
    ctx = context(tree, resolved, sandbox=LocalProcessSandbox(), allow_unisolated=True)
    tools = build_tools(agent_with(resolved, ["run_shell"]), ctx)

    def model(messages, info):
        return ModelResponse(parts=[ToolCallPart("run_shell", {"command": command})])

    step = BudgetLedger(Decimal("5")).run.open("build", "step", Decimal("1"))
    runner = PydanticAIRunner(
        resolved, FunctionModel(model), budget=step, state_dir=tmp_path, tools=tools, time_limit_s=1
    )
    outcome = asyncio.run(runner.run(AgentTask("x", {}, "go")))

    assert outcome.stopped == "time"
    time.sleep(5)
    assert not late.exists()  # the command died with the run instead of running on


# ── codeintel and skills ──────────────────────────────────────────────────────


def test_codeintel_tools_call_the_adapter(tree, resolved):
    tools = build_tools(agent_with(resolved, ["codeintel"]), context(tree, resolved))

    assert "tests/test_app.py" in call(tools, "tests_for", path="src/app.py")
    assert "src/uses_app.py" in call(tools, "dependents", path="src/app.py")


def test_activate_skill_only_for_the_agents_skills(tree, resolved):
    agent = agent_with(resolved, ["activate_skill"], skills=["superpowers/test-driven-development"])
    tools = build_tools(agent, context(tree, resolved))

    assert "failing test" in call(
        tools, "activate_skill", name="superpowers/test-driven-development"
    )
    assert call(tools, "activate_skill", name="evil/skill").startswith("REFUSED")


def test_testmodel_sees_every_offered_tool(tree, resolved):
    agent = agent_with(resolved, ["read_files", "search", "edit_files", "codeintel"])
    tools = build_tools(agent, context(tree, resolved))

    assert Agent(TestModel(call_tools=[]), tools=tools).run_sync("go").output
    assert {t.name for t in tools} == {
        "read_file",
        "list_dir",
        "search",
        "edit_file",
        "write_file",
        "dependents",
        "impact",
        "tests_for",
        "overview",
        "context_pack",
    }


# ── review fixes: tools never crash the run (I1), slow patterns (I2), edits (M5), names (M6)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("write_file", {"path": "src", "text": "x"}),  # onto a directory
        ("search", {"pattern": "x", "glob": "/**"}),  # an absolute glob
        ("search", {"pattern": "x", "glob": ""}),
        ("search", {"pattern": "x", "glob": "../**"}),
        ("search", {"pattern": "(unclosed", "glob": "**/*"}),
        ("read_file", {"path": "src"}),  # a directory, not a file
    ],
)
def test_a_bad_argument_is_refused_not_a_crash(tree, resolved, tool, args):
    tools = build_tools(
        agent_with(resolved, ["read_files", "search", "edit_files"]), context(tree, resolved)
    )

    assert call(tools, tool, **args).startswith("REFUSED")


def test_text_that_cannot_be_encoded_is_refused(tree, resolved):
    # a provider SDK can hand over a lone surrogate parsed from JSON; called directly
    # because Pydantic AI's own message serialiser cannot carry one
    tools = build_tools(agent_with(resolved, ["edit_files"]), context(tree, resolved))
    write = next(t for t in tools if t.name == "write_file")

    assert write.function(path="s.txt", text="\ud800").startswith("REFUSED")
    assert not (tree[1] / "s.txt").exists()


def test_a_catastrophic_pattern_is_refused_quickly(tree, resolved):
    (tree[1] / "src" / "slow.txt").write_text("a" * 60 + "!\n", encoding="utf-8")
    tools = build_tools(agent_with(resolved, ["search"]), context(tree, resolved))

    started = time.perf_counter()
    seen = call(tools, "search", pattern="(a|aa)+$")
    assert time.perf_counter() - started < 5
    assert seen.startswith("REFUSED") and "slow" in seen


def test_a_very_long_pattern_is_refused(tree, resolved):
    tools = build_tools(agent_with(resolved, ["search"]), context(tree, resolved))

    assert call(tools, "search", pattern="a" * 500).startswith("REFUSED")


def test_edit_keeps_windows_line_endings(tree, resolved):
    target = tree[1] / "src" / "crlf.py"
    target.write_bytes(b"def a():\r\n    return 1\r\n")
    tools = build_tools(agent_with(resolved, ["edit_files"]), context(tree, resolved))

    call(
        tools,
        "edit_file",
        path="src/crlf.py",
        old="def a():\n    return 1",
        new="def a():\n    return 2",
    )
    assert target.read_bytes() == b"def a():\r\n    return 2\r\n"


def test_edit_refuses_a_file_that_is_not_utf8(tree, resolved):
    target = tree[1] / "src" / "latin.txt"
    target.write_bytes("caf\xe9".encode("latin-1"))
    tools = build_tools(agent_with(resolved, ["edit_files"]), context(tree, resolved))

    assert call(tools, "edit_file", path="src/latin.txt", old="caf", new="cof").startswith(
        "REFUSED"
    )
    assert target.read_bytes() == "caf\xe9".encode("latin-1")


@pytest.mark.parametrize("name", ["CON", "nul", "aux.txt", "src/COM1.py"])
def test_windows_device_names_are_refused(tree, resolved, name):
    tools = build_tools(agent_with(resolved, ["edit_files"]), context(tree, resolved))

    assert call(tools, "write_file", path=name, text="x").startswith("REFUSED")
