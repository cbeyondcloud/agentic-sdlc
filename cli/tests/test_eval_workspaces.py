"""The trees a live Developer eval works in cannot read the corpus's answers (DF-392):
its reference solutions sit in the same corpus folder as the workspaces it starts from."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic.definitions import load_agent
from agentic.policy import ResolveContext, load_policy, resolve_agent
from agentic.sandbox import SrtSandbox
from agentic.sandbox.readiness import SandboxDecision
from agentic_cli import evaluation
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

LIBRARY = Path(__file__).resolve().parents[2] / "library" / "eval"
SRT = SandboxDecision(True, "srt 0.0.78", None, None)
NONE = SandboxDecision(False, "none", None, "no sandbox here: the run is on policy guards")


@pytest.fixture
def developer():
    policy = load_policy(LIBRARY / "fixtures" / "policy")
    definition = load_agent(LIBRARY / "fixtures" / "agents" / "developer.md")
    resolved, violations = resolve_agent(definition, policy, ResolveContext())
    assert resolved is not None, violations
    return resolved, policy


def decided(monkeypatch, decision: SandboxDecision) -> None:
    monkeypatch.setattr(evaluation, "machine_readiness", lambda: None)
    monkeypatch.setattr(evaluation, "decide_sandbox", lambda *args: decision)


def workspaces(developer, tmp_path, *, holdout=True, allow=False, corpus=LIBRARY):
    resolved, policy = developer
    return evaluation._workspaces(
        resolved,
        tmp_path / "out",
        "live",
        policy,
        corpus_root=corpus,
        holdout=holdout,
        allow_unsandboxed=allow,
    )


def test_a_sandboxed_eval_cannot_read_the_corpus(developer, tmp_path, monkeypatch):
    decided(monkeypatch, SRT)
    prepare, _, _ = workspaces(developer, tmp_path)

    workspace = prepare(SimpleNamespace(run_id="r-1"), LIBRARY / "workspaces" / "dev-slugify")

    assert isinstance(workspace.sandbox, SrtSandbox)
    rules = workspace.sandbox.rules
    assert LIBRARY.resolve() in rules.deny_read  # solutions, golden answers, the cases
    assert not (workspace.tree / "solutions").exists()  # the tree is the workspace only
    assert workspace.tree not in rules.allow_read  # outside the corpus: nothing to undo


def test_a_tree_inside_the_corpus_stays_readable(developer, tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    (corpus / "workspaces").mkdir(parents=True)
    decided(monkeypatch, SRT)
    resolved, policy = developer
    prepare, _, _ = evaluation._workspaces(
        resolved,
        corpus / "out",  # a report folder someone put inside the corpus
        "live",
        policy,
        corpus_root=corpus,
        holdout=True,
        allow_unsandboxed=False,
    )

    workspace = prepare(SimpleNamespace(run_id="r-1"), LIBRARY / "workspaces" / "dev-slugify")

    assert corpus.resolve() in workspace.sandbox.rules.deny_read
    assert workspace.tree.resolve() in workspace.sandbox.rules.allow_read


def test_an_unsandboxed_live_holdout_eval_is_refused(developer, tmp_path, monkeypatch):
    decided(monkeypatch, NONE)
    with pytest.raises(CliError) as refused:
        workspaces(developer, tmp_path)
    assert refused.value.code == ExitCode.REFUSED
    assert "solutions" in str(refused.value) and "--allow-unsandboxed" in refused.value.hint


def test_allow_unsandboxed_runs_and_says_the_answers_are_readable(developer, tmp_path, monkeypatch):
    decided(monkeypatch, NONE)
    _, _, notes = workspaces(developer, tmp_path, allow=True)
    assert any("readable" in note and "holdout" in note for note in notes)


def test_dev_cases_need_no_sandbox_and_no_flag(developer, tmp_path, monkeypatch):
    decided(monkeypatch, NONE)
    _, _, notes = workspaces(developer, tmp_path, holdout=False)
    assert not any("readable" in note for note in notes)
