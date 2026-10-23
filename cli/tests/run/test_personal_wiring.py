"""Personal skills and memory in the CLI (M2-45): only in the owner's runs, off where the
repo's classification says so, and every use stamped on the step's `step_finished` event
in the run ledger (kind, name, version, hash) - which the control plane receives through
the existing sink (DF-491: no new endpoint)."""

import asyncio

import pytest

from agentic.engine.engine import StepContext
from agentic.policy import resolve_agent
from agentic.skills import skill_digest
from agentic_cli.config import agentic_home
from agentic_cli.runtime import Runtime
from agentic_cli.skill_activity import SkillActivity, ledger_entry
from agentic_cli.step_extras import StepExtras
from agentic_cli.workspace import Workspace
from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import RunLedger

RUN = "r-personal-1"


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


@pytest.fixture
def habits(workspace):
    folder = agentic_home() / "skills" / "habits"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: habits\ndescription: How I like changes made.\n---\nKeep diffs small.\n",
        encoding="utf-8",
    )
    return folder


def agent(rt: Runtime, **update):
    definition = rt.ws.definitions().agent("ba")
    found, problems = resolve_agent(definition, rt.ws.policy(), rt.ws.context())
    assert found is not None, problems
    return found.model_copy(update=update)


def started(rt: Runtime) -> None:
    """A run this user started (its first step fails: the engine makes no model calls)."""
    asyncio.run(rt.engine(offline=True).start("deliver", {"request": "x"}, rt.user, RUN))


def test_step_finished_carries_activations(habits):
    rt = Runtime(Workspace.load())
    started(rt)
    text = StepExtras.for_runtime(rt)(StepContext(RUN, "design", 1, agent(rt), None, None))
    assert "### Personal skill: habits" in text and "Keep diffs small." in text

    rt.forwarding(RUN).record("step_finished", "design", outcome="completed")
    rt.close_sinks()
    last = RunLedger(rt.ws.repo, RUN).events()[-1]
    digest = skill_digest(habits).removeprefix("sha256:")
    assert last["ext"]["agentic"]["skills"] == [f"personal/habits/local/{digest}"]
    assert RunLedger(rt.ws.repo, RUN).verify() == []


def test_personal_only_in_owners_runs(habits, monkeypatch):
    owner = Runtime(Workspace.load())
    started(owner)
    monkeypatch.setenv("AGENTIC_USER", "someone-else@example.invalid")
    other = Runtime(Workspace.load())
    step = StepContext(RUN, "design", 1, agent(other), None, None)
    assert "Personal skill" not in StepExtras.for_runtime(other)(step)


def test_a_run_with_no_state_gets_no_personal_skills(habits):
    rt = Runtime(Workspace.load())
    step = StepContext("r-unknown", "design", 1, agent(rt), None, None)
    assert StepExtras.for_runtime(rt)(step) == ""


def test_blocked_in_restricted_repo_end_to_end(habits, policy_repo):
    classes = policy_repo / "policy" / "classifications.yaml"
    classes.write_text(
        "default_classification: internal\n"
        "classifications:\n"
        "  internal: { allowed_tiers: [balanced, fast], allowed_backends: [native] }\n"
        "  restricted: { allowed_tiers: [fast], allowed_backends: [native],"
        " personal_skills: false, personal_memory: false }\n",
        encoding="utf-8",
    )
    rt = Runtime(Workspace.load())
    started(rt)
    step = StepContext(RUN, "design", 1, agent(rt, classification="restricted"), None, None)
    assert StepExtras.for_runtime(rt)(step) == ""


def test_ledger_entries_fit_the_ledger_pattern():
    digest = "sha256:" + "d" * 64
    shared = {"skill": "acme/TDD_Rules", "version": "1.0+b2", "sha256": digest, "kind": "shared"}
    assert ledger_entry(shared) == "shared/acme/tdd_rules/1.0-b2/" + "d" * 64
    memory = {"skill": "memory/team", "version": "local", "sha256": digest, "kind": "memory"}
    assert ledger_entry(memory) == "memory/team/local/" + "d" * 64
    assert ledger_entry({"skill": "x", "kind": "personal"}) is None  # no hash: not recorded


def test_activity_is_taken_once_and_deduplicated():
    activity = SkillActivity()
    fields = {"skill": "personal/a", "version": "1", "sha256": "sha256:" + "e" * 64}
    activity.record("skills.activated", {**fields, "kind": "personal"})
    activity.record("skills.activated", {**fields, "kind": "personal"})
    activity.record("skills.unavailable", {"skill": "x"})
    assert activity.take() == ["personal/a/1/" + "e" * 64]
    assert activity.take() == []
