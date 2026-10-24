"""Pipeline overlays (M2-23, spec §6 and §6.1, D33).

A team's leads tailor an org pipeline for one repository with an overlay in the policy
repo: add steps, narrow gates, remove steps. An org-required step or gate can never be
removed or weakened, and a gate whose step was removed re-anchors to the nearest
remaining step rather than disappearing."""

from pathlib import Path

import pytest

from agentic.definitions import AgentStep, GateStep, LoopStep, PipelineDefinition
from agentic.definitions.overlay import (
    OverlayRefused,
    PipelineOverlay,
    apply_overlay,
    approved_overlays,
    load_overlay,
    overlay_applies,
    overlay_for,
)

BASE = {
    "name": "feature-delivery",
    "version": "1.0.0",
    "regime": "team",
    "max_minutes": 120,
    "steps": [
        {"id": "requirements", "agent": "business-analyst"},
        {"gate": "requirements-approval", "approver_role": "team-lead"},
        {"id": "baseline", "agent": "qa-baseline"},
        {"id": "design", "agent": "design"},
        {"gate": "design-approval", "approver_role": "team-lead"},
        {
            "loop": {
                "id": "build-review",
                "max_iterations": 3,
                "until": "review.open_blocking == 0",
                "steps": [
                    {"id": "develop", "agent": "developer"},
                    {"id": "review", "agent": "code-review"},
                    {"id": "qa", "agent": "qa"},
                ],
            }
        },
        {"gate": "review-approval", "approver_role": "approver", "on_reject": "design"},
        {"id": "publish", "action": "github.open_pr"},
    ],
}
REQUIRED = ["review", "review-approval"]


def base() -> PipelineDefinition:
    return PipelineDefinition.model_validate(BASE)


def names(pipeline: PipelineDefinition) -> list[str]:
    out = []
    for step in pipeline.steps:
        if isinstance(step, GateStep):
            out.append(step.gate)
        elif isinstance(step, LoopStep):
            out.append(f"{step.loop.id}[{','.join(s.id for s in step.loop.steps)}]")
        else:
            out.append(step.id)
    return out


def gate(pipeline: PipelineDefinition, name: str) -> GateStep:
    return next(s for s in pipeline.steps if isinstance(s, GateStep) and s.gate == name)


def overlay(**data) -> PipelineOverlay:
    return PipelineOverlay.model_validate(data)


def refused(change: PipelineOverlay, required=REQUIRED) -> str:
    with pytest.raises(OverlayRefused) as caught:
        apply_overlay(base(), change, required)
    return "\n".join(caught.value.errors)


# ── the two row tests ────────────────────────────────────────────────────────


def test_overlay_cannot_remove_required_gate():
    text = refused(overlay(remove=["review-approval"]))
    assert "review-approval" in text
    assert "rule required-step" in text and "required_steps" in text


def test_removed_step_reanchors_gate():
    result = apply_overlay(base(), overlay(remove=["design"]), REQUIRED)
    assert names(result.pipeline) == [
        "requirements",
        "requirements-approval",
        "baseline",
        "design-approval",  # stays, now after the previous remaining step
        "build-review[develop,review,qa]",
        "review-approval",
        "publish",
    ]
    assert "removed design" in result.notes
    assert "gate design-approval re-anchored from design to baseline" in result.notes


# ── re-anchoring at the edges ────────────────────────────────────────────────


def test_gate_at_head_moves_after_next_step():
    result = apply_overlay(base(), overlay(remove=["requirements"]), REQUIRED)
    assert names(result.pipeline)[:3] == ["baseline", "requirements-approval", "design"]
    assert (
        "gate requirements-approval re-anchored from requirements to baseline (moved after it)"
        in result.notes
    )


def test_on_reject_follows_the_new_anchor():
    """review-approval routed rejections to design; with design gone they go to the step
    now standing in its place, the nearest remaining step before it."""
    result = apply_overlay(base(), overlay(remove=["design"]), REQUIRED)
    assert gate(result.pipeline, "review-approval").on_reject == "baseline"
    assert "gate review-approval on_reject moved from design to baseline" in result.notes


def test_removed_loop_reanchors_the_gate_after_it():
    result = apply_overlay(base(), overlay(remove=["build-review"]), required=[])
    assert names(result.pipeline)[-3:] == ["design-approval", "review-approval", "publish"]
    assert "gate review-approval re-anchored from build-review to design" in result.notes


def test_a_removed_non_required_gate_is_gone():
    result = apply_overlay(base(), overlay(remove=["design-approval"]), REQUIRED)
    assert "design-approval" not in names(result.pipeline)


def test_removing_an_inner_step_keeps_the_loop():
    result = apply_overlay(base(), overlay(remove=["qa"]), REQUIRED)
    assert "build-review[develop,review]" in names(result.pipeline)


# ── never weaker ─────────────────────────────────────────────────────────────


def test_removing_a_loop_with_a_required_step_is_refused():
    text = refused(overlay(remove=["build-review"]))
    assert "build-review" in text and "review" in text and "rule required-step" in text


@pytest.mark.parametrize(
    ("removed", "required_gate"),
    [
        (["build-review"], "review-approval"),  # a required gate would approve design instead
        (["requirements"], "requirements-approval"),  # ... or approve the baseline
    ],
)
def test_removing_the_work_a_required_gate_approves_is_refused(removed, required_gate):
    """Review F1: re-anchoring a required gate onto another step is a weakening - it would
    approve work it was never meant to approve, so its anchor cannot be removed."""
    text = refused(overlay(remove=removed), required=[required_gate])
    assert "rule required-gate" in text and required_gate in text and removed[0] in text


def test_a_set_on_reject_must_name_an_earlier_step():
    """Review F2: an overlay's on_reject is checked where the overlay is applied, so the
    policy-repo gate names it, not each laptop's preflight."""
    for target in ("nonexist", "publish"):  # missing, and after the gate
        text = refused(overlay(gates={"review-approval": {"on_reject": target}}))
        assert "rule unknown-step" in text and repr(target) in text
    result = apply_overlay(base(), overlay(gates={"review-approval": {"on_reject": "develop"}}))
    assert gate(result.pipeline, "review-approval").on_reject == "develop"


def test_required_gate_approver_cannot_change():
    text = refused(overlay(gates={"review-approval": {"approver_role": "developer"}}))
    assert "rule required-gate" in text


def test_a_gate_approver_can_change_when_not_required():
    result = apply_overlay(
        base(), overlay(gates={"design-approval": {"approver_role": "architect"}}), REQUIRED
    )
    assert gate(result.pipeline, "design-approval").approver_role == "architect"
    assert "gate design-approval approver_role team-lead -> architect" in result.notes


def test_four_eyes_can_be_turned_on_even_on_a_required_gate():
    result = apply_overlay(
        base(), overlay(gates={"review-approval": {"four_eyes": True}}), REQUIRED
    )
    assert gate(result.pipeline, "review-approval").four_eyes is True


def test_four_eyes_cannot_be_turned_off():
    data = dict(BASE, steps=[*BASE["steps"]])
    data["steps"][4] = {"gate": "design-approval", "approver_role": "lead", "four_eyes": True}
    with pytest.raises(OverlayRefused, match="rule four-eyes"):
        apply_overlay(
            PipelineDefinition.model_validate(data),
            overlay(gates={"design-approval": {"four_eyes": False}}),
        )


def test_regime_only_tightens():
    assert "rule regime" in refused(overlay(regime="solo"))
    result = apply_overlay(base(), overlay(regime="regulated"), REQUIRED)
    assert result.pipeline.regime == "regulated"


def test_max_minutes_only_lowers():
    assert "rule max-minutes" in refused(overlay(max_minutes=500))
    assert apply_overlay(base(), overlay(max_minutes=60)).pipeline.max_minutes == 60


def test_gates_must_name_a_gate():
    assert "rule unknown-step" in refused(overlay(gates={"design": {"four_eyes": True}}))


# ── adding steps ─────────────────────────────────────────────────────────────


def test_add_after_and_before_including_inside_a_loop():
    change = overlay(
        add=[
            {"after": "review", "step": {"id": "security", "agent": "security-review"}},
            {"before": "publish", "step": {"gate": "release-approval", "approver_role": "lead"}},
        ]
    )
    result = apply_overlay(base(), change, REQUIRED)
    assert names(result.pipeline)[-4:] == [
        "build-review[develop,review,security,qa]",
        "review-approval",
        "release-approval",
        "publish",
    ]
    assert "added security after review" in result.notes
    assert "added release-approval before publish" in result.notes


def test_gate_added_inside_a_loop_is_invalid():
    change = overlay(add=[{"after": "develop", "step": {"gate": "g", "approver_role": "lead"}}])
    assert "rule invalid" in refused(change)


def test_an_added_step_needs_exactly_one_place():
    with pytest.raises(ValueError, match="exactly one of after or before"):
        overlay(add=[{"step": {"id": "x", "agent": "a"}}])


def test_a_duplicate_added_name_is_invalid():
    change = overlay(add=[{"after": "design", "step": {"id": "baseline", "agent": "a"}}])
    assert "rule invalid" in refused(change)


def test_unknown_step_named():
    text = refused(
        overlay(remove=["nope"], add=[{"after": "ghost", "step": {"id": "x", "agent": "a"}}])
    )
    assert "remove nope: rule unknown-step" in text
    assert "add x: rule unknown-step" in text


def test_adding_after_a_removed_step_is_unknown():
    change = overlay(
        remove=["design"], add=[{"after": "design", "step": {"id": "x", "agent": "a"}}]
    )
    assert "rule unknown-step" in refused(change)


def test_removing_the_last_loop_steps_is_refused():
    text = refused(overlay(remove=["develop", "review", "qa"]), required=[])
    assert "rule empty-loop" in text


def test_the_base_is_not_changed():
    pipeline = base()
    apply_overlay(pipeline, overlay(remove=["design"]), REQUIRED)
    assert "design" in names(pipeline)


def test_an_added_step_is_a_real_step():
    change = overlay(add=[{"after": "design", "step": {"id": "threat-model", "agent": "sec"}}])
    added = apply_overlay(base(), change).pipeline.steps[4]
    assert isinstance(added, AgentStep) and added.agent == "sec"


# ── the overlay file ─────────────────────────────────────────────────────────


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_overlay_file_loads(tmp_path: Path):
    path = write(
        tmp_path / "payments-api.yaml",
        "repo: git.example.com/acme/payments-api\n"
        "pipelines:\n  feature-delivery:\n    remove: [design]\n"
        "    gates: {review-approval: {four_eyes: true}}\n",
    )
    loaded = load_overlay(path)
    assert loaded.repo == "git.example.com/acme/payments-api"
    assert loaded.pipelines["feature-delivery"].remove == ["design"]


def test_overlay_file_with_unknown_key_is_refused(tmp_path: Path):
    path = write(tmp_path / "x.yaml", "pipelines:\n  p:\n    replace: {}\n")
    with pytest.raises(Exception, match="replace"):
        load_overlay(path)


@pytest.mark.parametrize(
    ("stem", "repo", "key", "applies"),
    [
        ("payments-api", None, "git.example.com/acme/payments-api", True),
        ("Payments-API", None, "git.example.com/acme/payments-api", True),  # any letter case
        ("payments-api", None, "git.example.com/other/payments-api", True),
        (
            "payments-api",
            "git.example.com/acme/payments-api",
            "git.example.com/other/payments-api",
            False,
        ),
        ("payments-api", None, "git.example.com/acme/payments", False),
        ("payments-api", None, None, False),  # no origin: no overlay
    ],
)
def test_overlay_file_matching_by_repo_name_and_key(stem, repo, key, applies):
    assert overlay_applies(stem, repo, key) is applies


def test_overlay_for_finds_the_team_file(tmp_path: Path):
    team = tmp_path / "teams" / "payments"
    found = write(team / "pipelines" / "overlays" / "payments-api.yaml", "pipelines: {}\n")
    write(team / "pipelines" / "overlays" / "other.yaml", "pipelines: {}\n")
    assert overlay_for(team, "git.example.com/acme/payments-api") == found
    assert overlay_for(team, "git.example.com/acme/billing") is None
    assert overlay_for(team, None) is None
    assert overlay_for(None, "git.example.com/acme/payments-api") is None


def test_an_overlay_cannot_change_failing_handoff(tmp_path: Path):
    """DF-263/DF-269: the completion opt-out belongs to the pipeline's owners, never to an
    overlay (it would let a failing QA report end a run `completed`)."""
    path = write(tmp_path / "x.yaml", "pipelines:\n  p:\n    failing_handoff: complete\n")
    with pytest.raises(OverlayRefused, match="failing_handoff"):
        load_overlay(path)


def test_approved_overlays_for_a_run():
    """The control plane's view (`overlay_index` entries): the overlays of the run's team
    that apply to its repository and change its pipeline."""
    index = {
        "teams/payments/pipelines/overlays/app.yaml": {
            "team": "payments",
            "repo": None,
            "pipelines": ["deliver"],
        },
        "teams/web/pipelines/overlays/app.yaml": {
            "team": "web",
            "repo": None,
            "pipelines": ["deliver"],
        },
        "teams/payments/pipelines/overlays/billing.yaml": {
            "team": "payments",
            "repo": "git.example.com/acme/billing",
            "pipelines": ["deliver", "hotfix"],
        },
    }
    key = "git.example.com/acme/App"
    found = approved_overlays(index, team="payments", repo=key, pipeline="deliver")
    assert found == ["teams/payments/pipelines/overlays/app.yaml"]
    assert approved_overlays(index, team="payments", repo=key, pipeline="hotfix") == []
    assert approved_overlays(index, team="payments", repo=None, pipeline="deliver") == []
    billing = approved_overlays(
        index, team="payments", repo="git.example.com/other/billing", pipeline="hotfix"
    )
    assert billing == []  # bound to acme/billing only
