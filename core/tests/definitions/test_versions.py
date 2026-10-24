"""Which version of an agent or pipeline a team gets (M2-15, spec §12.5): the team's pin,
else the previous version for a team outside the rollout, else the current file; then a
deprecated version warns before its date and is refused from it."""

from datetime import date
from pathlib import Path

from agentic.definitions.versions import (
    Deprecation,
    Deprecations,
    Pins,
    Rollout,
    RolloutEntry,
    VersionChoice,
    archive_path,
    archived,
    archived_version,
    choose_version,
    deprecation_state,
    version_problem,
)

ROLLOUT = Rollout(
    agents={"developer": RolloutEntry(version="1.3.0", previous="1.2.0", teams=["payments"])}
)
NO_PINS = Pins()
GONE = Deprecations(
    agents={"developer": {"1.2.0": Deprecation(blocked_from=date(2026, 12, 1), note="use 1.3.0")}}
)


def choose(team, pins=NO_PINS, name="developer"):
    return choose_version("agent", name, "1.3.0", rollout=ROLLOUT, pins=pins, team=team)


def test_team_not_in_rollout_keeps_previous_version():
    assert choose("platform") == ("1.2.0", "held")


def test_team_in_rollout_gets_new_version():
    assert choose("payments") == ("1.3.0", "current")


def test_no_team_is_outside_every_rollout():
    assert choose(None) == ("1.2.0", "held")


def test_definition_without_rollout_is_current_for_everyone():
    assert choose("platform", name="qa") == ("1.3.0", "current")


def test_pin_wins_over_rollout():
    pins = Pins(agents={"developer": "1.1.0"})
    assert choose("payments", pins) == ("1.1.0", "pinned")
    assert choose("platform", pins) == ("1.1.0", "pinned")


def test_deprecated_warns_then_blocks():
    """Warned the day before `blocked_from`; refused on it and after it."""
    before = deprecation_state("agent", "developer", "1.2.0", GONE, date(2026, 11, 30))
    on = deprecation_state("agent", "developer", "1.2.0", GONE, date(2026, 12, 1))
    after = deprecation_state("agent", "developer", "1.2.0", GONE, date(2027, 3, 1))
    assert before[0] is not None and before[1] is False
    assert on[1] is True and after[1] is True
    assert deprecation_state("agent", "developer", "1.3.0", GONE, date(2027, 3, 1)) == (
        None,
        False,
    )


def test_choice_describes_itself():
    held = VersionChoice(
        "agent",
        "developer",
        "1.2.0",
        "1.3.0",
        "held",
        GONE.agents["developer"]["1.2.0"],
        blocked=False,
    )
    assert held.describe() == (
        "agent developer@1.2.0 (your team is not in the rollout of 1.3.0): deprecated, "
        "refused from 2026-12-01 - use 1.3.0"
    )
    blocked = VersionChoice(
        "agent", "developer", "1.2.0", "1.3.0", "pinned", GONE.agents["developer"]["1.2.0"], True
    )
    assert "refused since 2026-12-01" in blocked.describe()


def problem(version, team, pins=NO_PINS, today=date(2026, 11, 1)):
    return version_problem(
        "agent",
        "developer",
        version,
        rollout=ROLLOUT,
        pins=pins,
        deprecated=GONE,
        team=team,
        today=today,
    )


def test_version_problem_for_the_control_plane():
    assert problem("1.3.0", "payments") is None
    assert problem("1.2.0", "platform") is None
    assert "not rolled out to team platform" in problem("1.3.0", "platform")
    assert "pins agent developer to 1.1.0" in problem(
        "1.3.0", "payments", Pins(agents={"developer": "1.1.0"})
    )
    assert problem("1.1.0", "payments", Pins(agents={"developer": "1.1.0"})) is None
    late = problem("1.2.0", "platform", today=date(2026, 12, 1))
    assert "deprecated and refused since 2026-12-01 - use 1.3.0" in late


def test_archived_versions_listed(tmp_path: Path):
    layer = tmp_path / "org"
    for version in ("1.1.0", "1.2.0"):
        path = archive_path(layer, "agent", "developer", version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    (layer / "agents" / "versions" / "developer" / ".gitkeep").write_text("", encoding="utf-8")
    found = archived(layer, "agent", "developer")
    assert sorted(found) == ["1.1.0", "1.2.0"]
    assert found["1.1.0"] == layer / "agents" / "versions" / "developer" / "1.1.0.md"
    assert archived(layer, "pipeline", "developer") == {}
    assert (
        archive_path(layer, "pipeline", "p", "2.0.0")
        .as_posix()
        .endswith("pipelines/versions/p/2.0.0.yaml")
    )


def test_archived_version_read_from_a_policy_repo_path():
    """The control plane learns a held-back pipeline's version from its approved path."""
    assert archived_version("pipelines/versions/deliver/1.0.0.yaml", "pipeline", "deliver") == (
        "1.0.0"
    )
    team = "teams/payments/pipelines/versions/deliver/2.1.0.yaml"
    assert archived_version(team, "pipeline", "deliver") == "2.1.0"
    assert archived_version("pipelines/deliver.yaml", "pipeline", "deliver") is None
    assert archived_version("pipelines/versions/other/1.0.0.yaml", "pipeline", "deliver") is None
    assert archived_version("agents/versions/deliver/1.0.0.md", "pipeline", "deliver") is None
