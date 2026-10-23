"""Personal skills and memory in the CLI (M2-45): only in the owner's runs, off where the
repo's classification says so, and every use stamped on the step's `step_finished` event
in the run ledger (kind, name, version, hash) - which the control plane receives through
the existing sink (DF-491: no new endpoint)."""

import asyncio
import logging
import re
from pathlib import Path

import pytest

from agentic.engine.engine import StepContext
from agentic.policy import resolve_agent
from agentic.skills import skill_digest
from agentic.skills.personal import MAX_PERSONAL_SKILLS
from agentic_cli.config import agentic_home
from agentic_cli.runtime import Runtime
from agentic_cli.skill_activity import SkillActivity, StampingRecorder, ledger_entry
from agentic_cli.step_extras import StepExtras
from agentic_cli.workspace import Workspace
from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import LedgerError, RunLedger

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


def test_preflight_warns_of_personal_skills_that_will_not_be_used(habits):
    junk = habits.parent / "with-script"
    junk.mkdir()
    (junk / "SKILL.md").write_text(
        "---\nname: with-script\ndescription: Has a script.\n---\nRun it.\n", encoding="utf-8"
    )
    (junk / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (habits / ".DS_Store").write_bytes(b"\x00\x01")  # OS junk: no warning
    for n in range(MAX_PERSONAL_SKILLS):
        extra = habits.parent / f"zz-extra-{n}"
        extra.mkdir()
        (extra / "SKILL.md").write_text(
            f"---\nname: zz-extra-{n}\ndescription: More.\n---\nMore.\n", encoding="utf-8"
        )
    report = Runtime(Workspace.load()).preflight("deliver", live=False)
    personal = [p for p in report.problems if p.check == "personal-skills"]
    assert personal and all(p.severity == "warning" for p in personal)
    text = "\n".join(p.detail for p in personal)
    assert "skills/with-script/run.sh" in text and "instruction-only" in text
    assert "habits" not in text  # it loads (its .DS_Store is ignored) and comes first
    assert f"zz-extra-{MAX_PERSONAL_SKILLS - 1}" in text
    assert f"at most {MAX_PERSONAL_SKILLS}" in text
    assert report.passed


def test_ledger_entries_fit_the_ledger_pattern():
    digest = "sha256:" + "d" * 64
    shared = {"skill": "acme/TDD_Rules", "version": "1.0+b2", "sha256": digest, "kind": "shared"}
    assert ledger_entry(shared) == "shared/acme/tdd_rules/1.0-b2/" + "d" * 64
    memory = {"skill": "memory/team", "version": "local", "sha256": digest, "kind": "memory"}
    assert ledger_entry(memory) == "memory/team/local/" + "d" * 64
    assert ledger_entry({"skill": "x", "kind": "personal"}) is None  # no hash: not recorded


LONG_NOTE = "2026-10-23-notes-on-the-payment-service-refactor-decisions"  # 58 characters


def fits_the_ledger(entry: str | None) -> str:
    assert entry is not None
    assert len(entry) <= 128, entry  # Zyroflo's id rule for every ext value
    assert re.fullmatch(ledger_module.FIELDS["skills"]["pattern"], entry), entry
    return entry


def test_long_names_and_versions_still_fit_the_ledger():
    digest = "d" * 64
    cases = [
        {"skill": f"memory/{LONG_NOTE}", "version": "local", "kind": "memory"},
        {"skill": "personal/" + "a" * 64, "version": "local", "kind": "personal"},
        {"skill": "personal/habits", "version": "1." + "9" * 98, "kind": "personal"},
        {"skill": "acme/" + "b" * 120, "version": "v" * 100, "kind": "shared"},
        {"skill": "memory/" + "/".join(["deep-folder"] * 12), "version": "local", "kind": "memory"},
    ]
    for case in cases:
        entry = fits_the_ledger(ledger_entry({**case, "sha256": f"sha256:{digest}"}))
        assert entry.endswith(f"/{digest}")  # the full content hash is always kept
        assert entry.startswith(case["kind"] + "/")
    # two long names that share their first characters stay two entries
    one = {"skill": "personal/" + "a" * 63 + "x", "kind": "personal", "sha256": digest}
    two = {**one, "skill": "personal/" + "a" * 63 + "y"}
    assert ledger_entry(one) != ledger_entry(two)


def test_a_long_note_name_does_not_break_step_finished(workspace):
    notes = agentic_home() / "memory"
    notes.mkdir(parents=True)
    (notes / f"{LONG_NOTE}.md").write_text("Prefer small pull requests.\n", encoding="utf-8")
    rt = Runtime(Workspace.load())
    started(rt)
    text = StepExtras.for_runtime(rt)(StepContext(RUN, "design", 1, agent(rt), None, None))
    assert "Prefer small pull requests." in text

    rt.forwarding(RUN).record("step_finished", "design", outcome="completed")
    rt.close_sinks()
    last = RunLedger(rt.ws.repo, RUN).events()[-1]
    assert last["event"] == "step_finished"
    [entry] = last["ext"]["agentic"]["skills"]
    assert fits_the_ledger(entry).startswith("memory/2026-10-23-notes-on-the-")


class RefusingRecorder:
    """A recorder whose ledger refuses any skills stamp (a future id-rule change)."""

    run_dir = Path(".")

    def __init__(self) -> None:
        self.recorded: list[tuple[str, dict]] = []

    def record(self, event: str, phase: str = "", **fields: object) -> object:
        if "skills" in fields:
            raise LedgerError("ext agentic.skills: not an id")
        self.recorded.append((event, fields))
        return {"event": event}


def test_a_refused_skills_stamp_never_loses_the_step_finished(caplog):
    activity = SkillActivity()
    fields = {"skill": "personal/a", "version": "1", "sha256": "sha256:" + "e" * 64}
    activity.record("skills.activated", {**fields, "kind": "personal"})
    inner = RefusingRecorder()
    with caplog.at_level(logging.INFO, logger="agentic.skills"):
        StampingRecorder(inner, activity).record("step_finished", "design", outcome="completed")
    assert inner.recorded == [("step_finished", {"outcome": "completed"})]
    assert any("skills.ledger_refused" in r.getMessage() for r in caplog.records)


def test_activity_is_taken_once_and_deduplicated():
    activity = SkillActivity()
    fields = {"skill": "personal/a", "version": "1", "sha256": "sha256:" + "e" * 64}
    activity.record("skills.activated", {**fields, "kind": "personal"})
    activity.record("skills.activated", {**fields, "kind": "personal"})
    activity.record("skills.unavailable", {"skill": "x"})
    assert activity.take() == ["personal/a/1/" + "e" * 64]
    assert activity.take() == []
