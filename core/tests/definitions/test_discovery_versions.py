"""Discovery picks each definition's version for the team and applies the team's overlay for
the repository (M2-15, M2-23): what a run of that team in that repository really gets."""

from datetime import date
from pathlib import Path

import pytest

from agentic.definitions import DefinitionError, DefinitionSet, GateStep
from agentic.definitions.discovery import PolicyRules
from agentic.definitions.versions import Deprecation, Deprecations, Pins, Rollout, RolloutEntry

REPO = "git.example.com/acme/payments-api"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def agent(name: str, version: str, description: str = "does it") -> str:
    return (
        f"---\nname: {name}\ntype: general\nversion: {version}\ndescription: {description}\n"
        "---\nDo it.\n"
    )


def pipeline(version: str, *, design: bool = True) -> str:
    steps = "  - id: build\n    agent: developer\n"
    if design:
        steps = "  - id: design\n    agent: developer\n  - gate: design-approval\n" + (
            "    approver_role: team-lead\n" + steps
        )
    ship = "  - gate: ship\n    approver_role: approver\n"
    return f"name: deliver\nversion: {version}\nsteps:\n{steps}{ship}"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A policy repo: developer 1.3.0 (1.2.0 archived), pipeline deliver 2.0.0 (1.0.0
    archived), team payments with an overlay for payments-api."""
    root = tmp_path / "org-policy"
    write(root / "agents" / "developer.md", agent("developer", "1.3.0", "new"))
    write(
        root / "agents" / "versions" / "developer" / "1.2.0.md", agent("developer", "1.2.0", "old")
    )
    write(root / "pipelines" / "deliver.yaml", pipeline("2.0.0"))
    write(
        root / "pipelines" / "versions" / "deliver" / "1.0.0.yaml", pipeline("1.0.0", design=False)
    )
    write(
        root / "teams" / "payments" / "pipelines" / "overlays" / "payments-api.yaml",
        "pipelines:\n  deliver:\n    remove: [design]\n    gates: {ship: {four_eyes: true}}\n",
    )
    (root / "teams" / "platform").mkdir(parents=True)
    return root


def rules(team, **extra) -> PolicyRules:
    defaults = {"team": team, "repo": REPO, "today": date(2026, 11, 1)}
    return PolicyRules(**{**defaults, **extra})


def discover(tree: Path, team: str, **extra) -> DefinitionSet:
    return DefinitionSet.discover(
        org=tree, team=tree / "teams" / team, repo=None, rules=rules(team, **extra)
    )


ROLLOUT = Rollout(
    agents={"developer": RolloutEntry(version="1.3.0", previous="1.2.0", teams=["payments"])}
)


def test_without_rules_the_current_files_win_and_no_overlay_applies(tree: Path):
    found = DefinitionSet.discover(org=tree, team=tree / "teams" / "payments", repo=None)
    assert found.agent("developer").version == "1.3.0"
    assert found.overlays == {} and found.versions == {}


def test_held_back_team_gets_the_archived_agent(tree: Path):
    found = discover(tree, "platform", rollout=ROLLOUT)
    assert found.agent("developer").version == "1.2.0"
    assert found.agent_file("developer").as_posix().endswith("agents/versions/developer/1.2.0.md")
    choice = found.versions[("agent", "developer")]
    assert (choice.reason, choice.current) == ("held", "1.3.0")


def test_team_in_rollout_gets_the_current_agent(tree: Path):
    found = discover(tree, "payments", rollout=ROLLOUT)
    assert found.agent("developer").version == "1.3.0"
    assert ("agent", "developer") not in found.versions  # nothing to say about it


def test_pinned_pipeline_read_from_archive(tree: Path):
    found = discover(tree, "platform", pins=Pins(pipelines={"deliver": "1.0.0"}))
    assert found.pipeline("deliver").version == "1.0.0"
    assert (
        found.pipeline_file("deliver").as_posix().endswith("pipelines/versions/deliver/1.0.0.yaml")
    )
    assert found.versions[("pipeline", "deliver")].reason == "pinned"


def test_missing_archive_is_a_definition_error(tree: Path):
    with pytest.raises(
        DefinitionError, match=r"agent developer@1\.1\.0 .*versions/developer/1\.1\.0\.md"
    ):
        discover(tree, "platform", pins=Pins(agents={"developer": "1.1.0"}))


def test_archived_file_must_match_its_path(tree: Path):
    write(tree / "agents" / "versions" / "developer" / "1.1.0.md", agent("developer", "1.0.9"))
    with pytest.raises(DefinitionError, match="version 1.0.9"):
        discover(tree, "platform", pins=Pins(agents={"developer": "1.1.0"}))


def test_deprecated_choice_is_recorded(tree: Path):
    gone = Deprecations(
        agents={"developer": {"1.3.0": Deprecation(blocked_from=date(2026, 12, 1))}}
    )
    early = discover(tree, "payments", deprecated=gone)
    late = discover(tree, "payments", deprecated=gone, today=date(2026, 12, 1))
    assert early.versions[("agent", "developer")].blocked is False
    assert late.versions[("agent", "developer")].blocked is True


def test_overlay_applied_for_the_repo(tree: Path):
    found = discover(tree, "payments")
    deliver = found.pipeline("deliver")
    assert [getattr(s, "id", None) or s.gate for s in deliver.steps] == [
        "build",
        "design-approval",
        "ship",
    ]
    ship = deliver.steps[-1]
    assert isinstance(ship, GateStep) and ship.four_eyes is True
    applied = found.overlays["deliver"]
    assert applied.file.as_posix().endswith("teams/payments/pipelines/overlays/payments-api.yaml")
    assert "gate design-approval re-anchored from design to build (moved after it)" in applied.notes
    assert found.overlay_file("deliver") == applied.file
    assert found.pipeline_file("deliver").as_posix().endswith("pipelines/deliver.yaml")  # base


def test_overlay_of_other_repo_not_applied(tree: Path):
    found = discover(tree, "payments", repo="git.example.com/acme/billing")
    assert found.overlays == {} and len(found.pipeline("deliver").steps) == 4


def test_overlay_of_another_team_not_applied(tree: Path):
    assert discover(tree, "platform").overlays == {}


def test_overlay_applies_to_the_version_the_team_gets(tree: Path):
    """Held on deliver 1.0.0 (no design step), the overlay removing design is refused:
    the team sees exactly which rule and which file."""
    with pytest.raises(DefinitionError, match="remove design: rule unknown-step") as caught:
        discover(tree, "payments", pins=Pins(pipelines={"deliver": "1.0.0"}))
    assert "payments-api.yaml" in str(caught.value)


def test_overlay_refusal_names_the_file_and_rule(tree: Path):
    with pytest.raises(DefinitionError, match="rule required-step") as caught:
        discover(tree, "payments", required_steps={"deliver": ["design"]})
    assert "payments-api.yaml" in str(caught.value)


def test_overlay_of_unknown_pipeline_is_an_error(tree: Path):
    write(
        tree / "teams" / "payments" / "pipelines" / "overlays" / "payments-api.yaml",
        "pipelines:\n  ghost:\n    remove: [x]\n",
    )
    with pytest.raises(DefinitionError, match="no pipeline named 'ghost'"):
        discover(tree, "payments")


def test_version_folders_are_not_definitions(tree: Path):
    """versions/ under agents/ and pipelines/ is the archive, never 'unexpected entries'."""
    found = DefinitionSet.discover(org=tree, team=None, repo=None)
    assert set(found.agents) == {"developer"} and set(found.pipelines) == {"deliver"}
