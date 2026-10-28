"""The code-review-graph adapter (M1-35; spike S5's composed answer) on a real fixture repo:
models <- service <- api, with tests for models and service."""

import sys

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agentic.codeintel import codeintel_tools
from agentic.codeintel.crg import CodeReviewGraph
from agentic.codeintel.index import LocalIndex, available

pytestmark = pytest.mark.skipif(not available(), reason="needs the codeintel extra")


@pytest.fixture
def intel(fixture_repo, tmp_path):
    index = LocalIndex(fixture_repo, tmp_path / "index", home_dir=tmp_path / "crg-home")
    return CodeReviewGraph(index)


def call(tools, name, **args) -> str:
    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart(str(returns[-1].content))])
        return ModelResponse(parts=[ToolCallPart(name, args)])

    return Agent(FunctionModel(model), tools=tools).run_sync("go").output


def test_dependents_of_models_are_service_api_and_tests(intel):
    found = intel.dependents("pkg/models.py")
    expected = {"pkg/service.py", "pkg/api.py", "tests/test_models.py", "tests/test_service.py"}
    assert expected <= set(found.files)
    assert "pkg/models.py" not in found.files  # never the seed itself


def test_tests_for_service_includes_its_test_file(intel):
    assert "tests/test_service.py" in intel.tests_for("pkg/service.py").files


def test_impact_reads_files_from_a_diff(intel):  # Review Focus 5
    diff = (
        "diff --git a/pkg/models.py b/pkg/models.py\n"
        "--- a/pkg/models.py\n+++ b/pkg/models.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/pkg/gone.py b/pkg/gone.py\n"
        "--- a/pkg/gone.py\n+++ /dev/null\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    found = intel.impact(diff)
    assert found.files[0] == "pkg/models.py"  # the diff's own files first
    assert {"pkg/service.py", "tests/test_models.py"} <= set(found.files)
    assert "pkg/gone.py" not in found.files and "logo.png" not in found.files


def test_overview_names_counts_and_hubs(intel):
    text = intel.overview()
    assert "files" in text and "pkg/models.py" in text


def test_answers_name_their_source(intel):
    assert intel.dependents("pkg/models.py").source == "code-review-graph"
    assert intel.supports == {"dependents", "impact", "tests_for", "overview"}


def test_a_failed_build_is_a_refusal_not_a_crash(fixture_repo, tmp_path):  # Review Focus 3
    failing = tmp_path / "failing_cli.py"
    failing.write_text("import sys\nprint('Error: parser crashed', file=sys.stderr)\nsys.exit(2)\n")
    index = LocalIndex(
        fixture_repo,
        tmp_path / "index",
        home_dir=tmp_path / "h",
        cli=[sys.executable, str(failing)],
    )
    tools = codeintel_tools(CodeReviewGraph(index), fixture_repo)
    out = call(tools, "dependents", path="pkg/models.py")
    assert out.startswith("REFUSED") and "parser crashed" in out


def test_the_tools_answer_through_the_adapter(intel, fixture_repo):
    out = call(codeintel_tools(intel, fixture_repo), "tests_for", path="pkg\\service.py")
    assert "tests/test_service.py" in out and "candidates from code-review-graph" in out
