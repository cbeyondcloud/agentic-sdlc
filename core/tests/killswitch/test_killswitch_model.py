"""The kill-switch model shared by the control plane and the CLI (M2-06, M2-19): scope
matching per kind, the team qualifier, kill over pause, and scope validation."""

import pytest

from agentic.killswitch import (
    RUN,
    SCOPE_KINDS,
    NoKillSwitch,
    RunKilled,
    RunScope,
    Switch,
    decide,
    matches,
    parse_scope,
    run_level,
    scope_problem,
    unconfirmed,
)

SCOPE = RunScope(
    team="payments",
    user="dev@acme.example",
    repo="github.com/acme/app",
    pipeline="deliver",
    agent="developer@1.2.0",
    model="acme/model-b",
    backend="native",
)


def switch(kind, value="", *, mode="kill", team=None, id="s1"):
    return Switch(id, kind, value, mode, "incident 42", "lead@acme.example", team=team)


@pytest.mark.parametrize(
    ("kind", "hit", "miss"),
    [
        ("team", "payments", "search"),
        ("user", "dev@acme.example", "other@acme.example"),
        ("repo", "github.com/acme/app", "github.com/acme/other"),
        ("pipeline", "deliver", "hotfix"),
        ("agent", "developer", "reviewer"),
        ("model", "acme/model-b", "acme/model-f"),
        ("backend", "native", "acp"),
    ],
)
def test_scope_matching_per_kind(kind, hit, miss):
    assert matches(switch(kind, hit), SCOPE)
    assert not matches(switch(kind, miss), SCOPE)


def test_tenant_switch_matches_every_run():
    assert matches(switch("tenant"), SCOPE)
    assert matches(switch("tenant"), RunScope())


def test_agent_by_name_or_exact_version():
    assert matches(switch("agent", "developer"), SCOPE)
    assert matches(switch("agent", "developer@1.2.0"), SCOPE)
    assert not matches(switch("agent", "developer@1.3.0"), SCOPE)
    assert not matches(switch("agent", "dev"), SCOPE)  # a prefix is not the name


def test_step_dimensions_unknown_never_match():
    """Before a step is known (no agent, model or backend), only run-level kinds match."""
    run = RunScope(team="payments", pipeline="deliver")
    assert not matches(switch("agent", "developer"), run)
    assert not matches(switch("model", "acme/model-b"), run)


def test_team_qualifier_limits_any_kind():
    assert matches(switch("model", "acme/model-b", team="payments"), SCOPE)
    assert not matches(switch("model", "acme/model-b", team="search"), SCOPE)
    assert not matches(switch("tenant", team="search"), SCOPE)
    assert not matches(switch("model", "acme/model-b", team="payments"), RunScope(model="x"))


def test_run_level_treats_step_kinds_as_possible():
    assert run_level(switch("agent", "anything"), RunScope(team="payments"))
    assert run_level(switch("backend", "x", team="payments"), RunScope(team="payments"))
    assert not run_level(switch("backend", "x", team="search"), RunScope(team="payments"))
    assert not run_level(switch("pipeline", "hotfix"), RunScope(pipeline="deliver"))


def test_kill_beats_pause_and_no_switch_runs():
    pause = switch("team", "payments", mode="pause", id="p")
    kill = switch("pipeline", "deliver", mode="kill", id="k")
    assert decide([pause, kill], SCOPE).action == "kill"
    assert decide([pause, kill], SCOPE).switch == "k"
    held = decide([pause], SCOPE)
    assert (held.action, held.pause_id, held.by) == ("pause", "hold", "lead@acme.example")
    assert decide([switch("team", "search")], SCOPE) == RUN
    assert decide([], SCOPE) == RUN


def test_unconfirmed_state_is_a_pause_not_a_kill():
    verdict = unconfirmed("the control plane has not answered for 75 s")
    assert verdict.action == "pause" and verdict.stale
    assert verdict.pause_id == "kill-switch-unconfirmed"
    assert "75 s" in verdict.describe()


def test_verdict_describes_who_and_why():
    text = decide([switch("team", "payments")], SCOPE).describe()
    assert "incident 42" in text and "lead@acme.example" in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("global", ("tenant", "")),
        ("team:payments", ("team", "payments")),
        ("user:Dev@Acme.example", ("user", "dev@acme.example")),
        ("repo:https://github.com/Acme/App.git", ("repo", "github.com/acme/app")),
        ("agent:developer@1.2.0", ("agent", "developer@1.2.0")),
        ("model:acme/model-b", ("model", "acme/model-b")),
        ("backend:native", ("backend", "native")),
    ],
)
def test_parse_scope_normalises(text, expected):
    assert parse_scope(text) == expected


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("team", "Payments"),
        ("team", ""),
        ("tenant", "x"),
        ("user", "not-an-email"),
        ("model", "has space"),
        ("agent", "dev@"),
        ("pipeline", ""),
        ("nonsense", "x"),
    ],
)
def test_scope_problem_refuses_bad_values(kind, value):
    assert scope_problem(kind, value)


@pytest.mark.parametrize(
    "text",
    [
        "repo:github.com/acme/*",
        "repo:github.com/acme/app?",
        "repo:github.com/acme/[ab]pp",
        "model:acme/*",
        "backend:nat?ve",
    ],
)
def test_a_wildcard_scope_is_refused_since_switches_match_exactly(text):
    """Review F5: `repo:github.com/acme/*` would be stored, match no run, and leave the
    operator believing the whole org was stopped."""
    with pytest.raises(ValueError, match="wildcard"):
        parse_scope(text)


def test_parse_scope_refuses_unknown_kind():
    with pytest.raises(ValueError, match="scope"):
        parse_scope("planet:mars")


def test_every_kind_is_known():
    expected = ("tenant", "team", "user", "repo", "pipeline", "agent", "model", "backend")
    assert SCOPE_KINDS == expected


def test_no_kill_switch_always_runs_and_run_killed_carries_verdict():
    assert NoKillSwitch().check(SCOPE) == RUN
    verdict = decide([switch("tenant")], SCOPE)
    error = RunKilled(verdict)
    assert error.verdict is verdict and "incident 42" in str(error)
