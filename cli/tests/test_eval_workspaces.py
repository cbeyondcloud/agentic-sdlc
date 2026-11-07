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
    # with a shell: the fixture Developer has none, and a shell is what can leave the tree
    return resolved.model_copy(update={"tools": [*resolved.tools, "run_shell"]}), policy


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


def test_dev_cases_need_no_sandbox_and_no_flag_but_are_warned(developer, tmp_path, monkeypatch):
    decided(monkeypatch, NONE)
    _, _, notes = workspaces(developer, tmp_path, holdout=False)
    assert not any(note.startswith(evaluation.EXPOSED_NOTE) for note in notes)
    # review M7: the corpus still holds the holdout answers beside the dev ones
    assert any("readable" in note and "holdout" in note for note in notes)


def test_the_refusal_names_wsl2_and_doctor_on_windows(developer, tmp_path, monkeypatch):  # M8
    decided(monkeypatch, NONE)
    monkeypatch.setattr(evaluation, "_on_windows", lambda: True)
    with pytest.raises(CliError) as refused:
        workspaces(developer, tmp_path)
    assert "WSL2" in refused.value.hint and "agentic doctor" in refused.value.hint


def test_skills_eval_takes_allow_unsandboxed(capsys, monkeypatch):  # review M7
    from agentic_cli.main import main

    monkeypatch.setenv("COLUMNS", "200")

    main(["skills", "eval", "--help"])
    assert "--allow-unsandboxed" in capsys.readouterr().out


def test_an_agent_without_a_shell_needs_no_sandbox(developer, tmp_path, monkeypatch):
    """Its file tools are confined to its tree: with no run_shell nothing it runs can
    reach the corpus (review)."""
    resolved, policy = developer
    tools = [t for t in resolved.tools if t != "run_shell"]
    decided(monkeypatch, NONE)
    evaluation._workspaces(
        resolved.model_copy(update={"tools": tools}),
        tmp_path / "out",
        "live",
        policy,
        corpus_root=LIBRARY,
        holdout=True,
        allow_unsandboxed=False,
    )


def _developer_upstream(request):
    """A scripted gateway: the Developer answers its dev-report without touching files."""
    import json

    from agentic.eval.replay import UpstreamReply

    answer = (LIBRARY / "golden" / "dev-report").glob("*.md")
    body = json.loads(request.body)
    reply = {
        "id": "c",
        "object": "chat.completion",
        "created": 1,
        "model": body["model"],
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": next(answer).read_text("utf-8")},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return UpstreamReply(200, json.dumps(reply).encode(), "application/json")


def _developer_run(tmp_path, monkeypatch, **options):
    decided(monkeypatch, NONE)
    real = evaluation.resolve_agent

    def with_shell(definition, policy, context):  # the fixture Developer has no shell
        resolved, violations = real(definition, policy, context)
        tools = [*resolved.tools, "run_shell"]
        return resolved.model_copy(update={"tools": tools}), violations

    monkeypatch.setattr(evaluation, "resolve_agent", with_shell)
    return evaluation.RunOptions(
        agent="developer",
        corpus=LIBRARY,
        mode="record",
        tag="fixture",
        out=tmp_path / "o",
        cassettes=tmp_path / "cassettes",
        **options,
    )


def test_a_holdout_run_without_srt_is_refused_end_to_end(tmp_path, monkeypatch):
    options = _developer_run(tmp_path, monkeypatch)
    with pytest.raises(CliError, match="readable"):
        evaluation.execute_run(options, upstream=_developer_upstream)


def test_an_allowed_unsandboxed_report_says_its_answers_were_exposed(tmp_path, monkeypatch):
    options = _developer_run(tmp_path, monkeypatch, allow_unsandboxed=True)
    result = evaluation.execute_run(options, upstream=_developer_upstream)
    assert result.report.holdout_exposed is True  # DF-1006: `eval gate` refuses it
