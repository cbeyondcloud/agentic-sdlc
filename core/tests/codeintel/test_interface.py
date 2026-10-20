"""The codeintel interface and its agent tools (M1-34; spec §10.1): only supported operations
are offered, paths stay in the tree, answers are capped and labelled candidates."""

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

from agentic.codeintel import Candidates, codeintel_tools

ALL = frozenset({"dependents", "impact", "tests_for", "overview"})


class FakeIntel:
    name = "fake"

    def __init__(self, supports=ALL, many: int = 0) -> None:
        self.supports = frozenset(supports)
        self.calls: list[tuple[str, str]] = []
        self.many = many

    def _answer(self, op: str, arg: str, files: list[str]) -> Candidates:
        self.calls.append((op, arg))
        if self.many:
            files = [f"pkg/m{i}.py" for i in range(self.many)]
        return Candidates(tuple(files), source=self.name)

    def dependents(self, path: str) -> Candidates:
        return self._answer("dependents", path, ["pkg/service.py", "tests/test_models.py"])

    def impact(self, diff: str) -> Candidates:
        return self._answer("impact", diff, ["pkg/api.py"])

    def tests_for(self, path: str) -> Candidates:
        return self._answer("tests_for", path, ["tests/test_models.py"])

    def overview(self) -> str:
        self.calls.append(("overview", ""))
        return "3 files, 2 tests"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "models.py").write_text("class Model:\n    pass\n" * 50, "utf-8")
    return tmp_path


def call(tools, name: str, **args) -> str:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart(str(returns[-1].content))])
        return ModelResponse(parts=[ToolCallPart(name, args)])

    return Agent(FunctionModel(model), tools=tools).run_sync("go").output


def names(tools) -> set[str]:
    return {t.name for t in tools}


def test_each_supported_operation_is_a_tool(tree):
    tools = codeintel_tools(FakeIntel(), tree)
    assert names(tools) == {"dependents", "impact", "tests_for", "overview", "context_pack"}


def test_unsupported_operations_are_not_offered(tree):
    assert names(codeintel_tools(FakeIntel({"tests_for"}), tree)) == {"tests_for", "context_pack"}
    assert names(codeintel_tools(FakeIntel({"overview"}), tree)) == {"overview"}


def test_answers_are_labelled_candidates(tree):
    out = call(codeintel_tools(FakeIntel(), tree), "dependents", path="pkg/models.py")
    assert "pkg/service.py" in out
    assert out.rstrip().endswith("(candidates from fake - verify before relying on them)")


@pytest.mark.parametrize("spelling", ["pkg\\models.py", "./pkg/models.py", "pkg//models.py"])
def test_paths_are_normalised_and_confined(tree, spelling):  # Review Focus 1
    intel = FakeIntel()
    call(codeintel_tools(intel, tree), "tests_for", path=spelling)
    call(codeintel_tools(intel, tree), "tests_for", path=str(tree / "pkg" / "models.py"))
    assert intel.calls == [("tests_for", "pkg/models.py")] * 2


@pytest.mark.parametrize("outside", ["../secret.py", "/etc/passwd", "C:/Windows/win.ini"])
def test_a_path_outside_the_tree_is_refused(tree, outside):
    intel = FakeIntel()
    assert call(codeintel_tools(intel, tree), "dependents", path=outside).startswith("REFUSED")
    assert intel.calls == []


def test_large_answers_are_capped(tree):  # Review Focus 4
    out = call(codeintel_tools(FakeIntel(many=300), tree), "dependents", path="pkg/models.py")
    listed = [line for line in out.splitlines() if line.startswith("pkg/m")]
    assert len(listed) == 50
    assert "+250 more" in out


def test_impact_takes_a_diff(tree):
    intel = FakeIntel()
    out = call(
        codeintel_tools(intel, tree), "impact", diff="--- a/pkg/models.py\n+++ b/pkg/models.py\n"
    )
    assert "pkg/api.py" in out and intel.calls[0][0] == "impact"


def test_overview_is_text(tree):
    assert "3 files" in call(codeintel_tools(FakeIntel(), tree), "overview")


def test_context_pack_stays_within_its_budget(tree):
    out = call(
        codeintel_tools(FakeIntel(), tree),
        "context_pack",
        path="pkg/models.py",
        budget_chars=400,
    )
    assert len(out) <= 400 + 200  # the budget, plus the candidates label and headings
    assert "class Model" in out
    assert "tests/test_models.py" in out and "pkg/service.py" in out


def test_context_pack_of_a_missing_file_is_refused(tree):
    out = call(codeintel_tools(FakeIntel(), tree), "context_pack", path="pkg/nope.py")
    assert out.startswith("REFUSED")
