"""The Code Review agent receives the deterministic pre-pass (M2-48, DF-228): when its
step runs, the run's change is scanned and the results end its instructions; other agents
get nothing; a scanner that is not installed is reported as not run, with the reason."""

import json

from agentic.engine.engine import StepContext
from agentic.policy import resolve_agent
from agentic_cli.runtime import Runtime
from agentic_cli.step_extras import StepExtras
from agentic_cli.workspace import Workspace
from sdlc_commons import ledger as ledger_module

AWS = "AKIA" + "Z7Q4R2XW" + "9PLM3TKB"
RUN = "r-prepass-1"


def org_yaml(policy_repo, extra: str) -> None:
    org = policy_repo / "policy" / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + extra, encoding="utf-8")


def agent(rt: Runtime, agent_type: str):
    definition = rt.ws.definitions().agent("ba")
    found, problems = resolve_agent(definition, rt.ws.policy(), rt.ws.context())
    assert found is not None, problems
    return found.model_copy(update={"type": agent_type})


def developed(rt: Runtime, monkeypatch) -> None:
    """A run whose Developer step committed a seeded secret on the run branch."""
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})
    rt.recorder(RUN)
    engine = rt.engine(offline=True)
    trees = engine.trees_for(RUN)
    tree = trees.prepare()
    trees.begin(fresh=True)
    (tree / "src" / "settings.py").write_text(f'KEY = "{AWS}"\n', encoding="utf-8")
    trees.finish("agentic: develop #1")


def test_code_review_step_gets_prepass_findings(workspace, monkeypatch):
    rt = Runtime(Workspace.load())
    developed(rt, monkeypatch)
    extras = StepExtras.for_runtime(rt)

    text = extras(StepContext(RUN, "review", 1, agent(rt, "code-review"), None, None))
    assert "## Pre-pass results" in text
    assert "- secret-scan: ran, 1 finding" in text
    assert "`src/settings.py:1` [ERROR] AWSKeyDetector" in text
    assert "- gitleaks: DID NOT RUN (not-run) - not run: org policy pins no gitleaks" in text
    assert "- osv-scanner: DID NOT RUN" in text
    assert AWS not in text
    report = rt.recorder(RUN).folder / "prepass" / "review-1.json"
    assert json.loads(report.read_text(encoding="utf-8"))["summary"]["total"] == 1

    assert extras(StepContext(RUN, "develop", 2, agent(rt, "general"), None, None)) == ""


def test_prepass_off_by_policy(workspace, policy_repo, monkeypatch):
    org_yaml(policy_repo, "review_prepass:\n  enabled: false\n")
    rt = Runtime(Workspace.load())
    developed(rt, monkeypatch)
    review = StepContext(RUN, "review", 1, agent(rt, "code-review"), None, None)
    assert StepExtras.for_runtime(rt)(review) == ""


def test_a_run_without_trees_says_the_prepass_could_not_run(workspace, monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})
    rt = Runtime(Workspace.load())
    rt.recorder("r-no-trees")
    review = StepContext("r-no-trees", "review", 1, agent(rt, "code-review"), None, None)
    text = StepExtras.for_runtime(rt)(review)
    assert "The pre-pass did not run" in text and "not a clean result" in text


def test_the_engine_gives_step_runners_the_extras(workspace, monkeypatch):
    from agentic_cli import runtime

    seen = {}

    class Recording(runtime.StepRunners):
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(runtime, "StepRunners", Recording)
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", "tl-" + "fake-key-0002")  # gitleaks:allow
    Runtime(Workspace.load()).engine()
    assert isinstance(seen["prompt_extras"], StepExtras)
