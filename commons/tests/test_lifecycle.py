"""Tests for the lifecycle loader over Zyroflo's packaged content (M1-10)."""

from pathlib import Path

import pytest
from tools.sdlc import content, state

from sdlc_commons import lifecycle
from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.lifecycle import LifecycleError

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)

PHASES = ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "P11"]


# ── stages ────────────────────────────────────────────────────────────────────


def test_all_twelve_stages_load_in_order():
    stages = lifecycle.stages()

    assert [s.phase for s in stages] == PHASES
    assert [s.name for s in stages][:4] == ["intake", "requirements", "scoping", "design"]


def test_each_stage_carries_its_front_matter_and_instructions():
    review = lifecycle.stage("P6")

    assert review.name == "review"
    assert "60-review.md" in review.produces
    assert "prepass.json" in review.evidence
    assert review.instructions.startswith("# P6")
    assert "---" not in review.instructions.splitlines()[0]
    assert review.sop.is_file()


def test_gates_are_where_the_lifecycle_puts_them():
    gates = {s.phase: s.gate for s in lifecycle.stages() if s.gate}

    assert set(gates.values()) == {"G1", "G2", "G3", "G4", "G5"}


def test_stages_are_read_from_the_installed_package_not_a_checkout():
    assert content.installed()
    assert all(s.sop.is_relative_to(content.root()) for s in lifecycle.stages())


def test_an_unknown_phase_is_refused():
    with pytest.raises(LifecycleError, match="P42"):
        lifecycle.stage("P42")


def test_an_sop_whose_front_matter_disagrees_with_the_catalog_is_refused(tmp_path, monkeypatch):
    stages = tmp_path / "sdlc" / "stages"
    stages.mkdir(parents=True)
    for s in lifecycle.stages():
        text = s.sop.read_text(encoding="utf-8")
        if s.phase == "P6":
            text = text.replace("outputs: [60-review.md]", "outputs: [something-else.md]")
        (stages / s.sop.name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(content, "root", lambda: tmp_path)

    with pytest.raises(LifecycleError, match="P6"):
        lifecycle.stages()


def test_a_missing_sop_is_refused(tmp_path, monkeypatch):
    (tmp_path / "sdlc" / "stages").mkdir(parents=True)
    monkeypatch.setattr(content, "root", lambda: tmp_path)

    with pytest.raises(LifecycleError, match="P0"):
        lifecycle.stages()


# ── review rubric ─────────────────────────────────────────────────────────────


def test_the_review_rubric_has_six_categories_whose_weights_sum_to_ten():
    rubric = lifecycle.review_rubric()

    assert [c.name for c in rubric][:2] == ["Logical correctness", "Security"]
    assert len(rubric) == 6
    assert sum(c.weight for c in rubric) == pytest.approx(10.0)
    assert all(c.checks for c in rubric)


def test_a_rubric_whose_weights_do_not_sum_to_ten_is_refused(tmp_path, monkeypatch):
    real = (content.root() / "sdlc_templates" / "review-rubric.md").read_text(encoding="utf-8")
    broken = tmp_path / "sdlc_templates" / "review-rubric.md"
    broken.parent.mkdir(parents=True)
    broken.write_text(real.replace("| 2.5 |", "| 3.5 |"), encoding="utf-8")
    monkeypatch.setattr(content, "root", lambda: tmp_path)

    with pytest.raises(LifecycleError, match="11"):
        lifecycle.review_rubric()


# ── run folders ───────────────────────────────────────────────────────────────


def test_run_folders_live_under_agentic_local_runs(tmp_path: Path):
    ledger = RunLedger(tmp_path, "r-local")
    ledger.open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )

    assert state.RUNS_REL.as_posix() == lifecycle.RUNS_ROOT == ".agentic/local/runs"
    assert ledger.folder == tmp_path / ".agentic" / "local" / "runs" / "r-local"
    assert (ledger.folder / "events.jsonl").is_file()
    assert not (tmp_path / ".sdlc" / "runs").exists()
