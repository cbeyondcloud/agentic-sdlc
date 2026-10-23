"""Admin commands for kill and pause switches (M2-25): `agentic killswitch on|off|list`,
`agentic pause <scope>` and `agentic resume --scope <scope>`, with the server's refusal per
role shown as exit 2."""

from __future__ import annotations

import json

import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic_cli import control_plane
from agentic_cli.config import agentic_home
from agentic_cli.main import main


@pytest.fixture
def plane(home, monkeypatch) -> FakeControlPlane:
    """A signed-in org admin of tenant acme (the token from the environment, as in CI)."""
    fake = FakeControlPlane(allowed={"killswitch.global", "killswitch.team"})
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    config = agentic_home()
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        f"org: acme\ncontrol_plane_url: {CP_URL}\n", encoding="utf-8"
    )
    return fake


def agentic(capsys, *args: str) -> tuple[int, dict | None, str]:
    code = main(["--json", *args])
    captured = capsys.readouterr()
    try:
        data = json.loads(captured.out) if captured.out.strip() else None
    except ValueError:
        data = None
    return code, data, captured.err


def posted(plane, path="/v1/killswitch"):
    return [
        json.loads(r.content) for r in plane.requests if r.url.path == path and r.method == "POST"
    ]


def test_killswitch_on_sets_a_kill_switch(plane, capsys):
    code, data, _ = agentic(capsys, "killswitch", "on", "team:payments", "--reason", "incident 42")
    assert code == 0, data
    assert data["scope_kind"] == "team" and data["mode"] == "kill" and data["active"]
    assert posted(plane) == [
        {
            "scope_kind": "team",
            "scope_value": "payments",
            "mode": "kill",
            "reason": "incident 42",
        }
    ]


def test_pause_sets_a_pause_switch_limited_to_a_team(plane, capsys):
    code, data, _ = agentic(
        capsys, "pause", "model:acme/model-b", "--team", "payments", "--reason", "upgrade"
    )
    assert code == 0 and data["mode"] == "pause"
    assert posted(plane)[0]["team"] == "payments"


def test_global_scope_is_tenant_wide(plane, capsys):
    code, _, _ = agentic(capsys, "killswitch", "on", "global", "--reason", "stop everything")
    assert code == 0 and posted(plane)[0]["scope_kind"] == "tenant"


def test_killswitch_off_and_list(plane, capsys):
    made = plane.add_switch("team", "payments", reason="incident")
    plane.add_switch("tenant", reason="all")
    code, data, _ = agentic(capsys, "killswitch", "list")
    assert code == 0 and [s["id"] for s in data["switches"]] == [
        made["id"],
        plane.switches[1]["id"],
    ]
    code, data, _ = agentic(capsys, "killswitch", "off", made["id"], "--reason", "fixed")
    assert code == 0 and data["active"] is False
    deleted = [r for r in plane.requests if r.method == "DELETE"]
    assert deleted[0].url.params["reason"] == "fixed"


def test_resume_scope_clears_the_pause_switches_of_that_scope(plane, capsys):
    pause = plane.add_switch("team", "payments", mode="pause", reason="freeze")
    kill = plane.add_switch("team", "payments", mode="kill", reason="incident")
    other = plane.add_switch("team", "search", mode="pause", reason="freeze")
    code, data, _ = agentic(capsys, "resume", "--scope", "team:payments")
    assert code == 0 and data["cleared"] == [pause["id"]]
    assert kill["active"] and other["active"] and not pause["active"]


def test_resume_scope_with_nothing_paused_is_an_error(plane, capsys):
    code, _, err = agentic(capsys, "resume", "--scope", "team:payments")
    assert code == 1 and "no pause switch" in err


def test_a_developer_is_refused_with_exit_2(plane, capsys):
    plane.allowed = set()
    code, _, err = agentic(capsys, "killswitch", "on", "team:payments", "--reason", "nope")
    assert code == 2 and "killswitch" in err


def test_a_team_lead_is_refused_outside_their_team(plane, capsys):
    plane.allowed, plane.lead_teams = set(), {"payments"}
    assert agentic(capsys, "pause", "team:payments", "--reason", "own team")[0] == 0
    code, _, _ = agentic(capsys, "pause", "team:search", "--reason", "other team")
    assert code == 2
    code, _, _ = agentic(capsys, "killswitch", "on", "global", "--reason", "everyone")
    assert code == 2


def test_a_bad_scope_is_a_usage_error(plane, capsys):
    code, _, err = agentic(capsys, "killswitch", "on", "planet:mars", "--reason", "x")
    assert code == 64 and "scope" in err
    assert not posted(plane)


def test_resume_takes_a_run_or_a_scope_not_both(plane, capsys):
    code, _, _ = agentic(capsys, "resume", "r-1", "--scope", "team:payments")
    assert code == 64
    code, _, _ = agentic(capsys, "resume")
    assert code == 64


def test_outside_org_mode_the_commands_are_refused(home, capsys, monkeypatch):
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    code, _, err = agentic(capsys, "killswitch", "list")
    assert code == 2 and "control plane" in err
