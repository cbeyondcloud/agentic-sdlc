"""The learning loop in org mode (M3-11, M3-12): gate feedback reaches the control plane
through the feedback sink (after the run's own events), and an attribution correction goes
to the control plane's audited endpoint. A fake control plane answers from memory."""

import json
from datetime import UTC, datetime

import pytest
from _cli_support import FakeGateway, answer, git
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic.learning.records import FeedbackRecord, feedback_id
from agentic_cli import control_plane, learning, runtime
from agentic_cli.exit_codes import ExitCode
from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace

KEY = "tl-" + "fake-gateway-key-0312"  # gitleaks:allow (not a real key)


@pytest.fixture
def org(workspace, tmp_path, monkeypatch) -> FakeControlPlane:
    fake = FakeControlPlane(email="dev@example.invalid")
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    git(workspace, "remote", "add", "origin", "https://github.com/acme/app.git")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(f"org: acme\ncontrol_plane_url: {CP_URL}\n", "utf-8")
    gateway = FakeGateway({"ba": [answer("requirements", summary=f"Try {n}.") for n in range(3)]})
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", gateway.transport)
    return fake


def test_feedback_is_forwarded_to_the_control_plane(org, cli):
    started = cli("--json", "run", "deliver", "--input", "request=CSV export")
    run_id = started.json()["run_id"]
    org.assignments = [{"role": "team-lead", "scope": "team", "team": "payments"}]
    result = cli("--json", "reject", run_id, "--feedback", "Name every CSV column.")
    assert result.code == ExitCode.PAUSED, result.err
    (entry,) = org.events[f"feedback:{run_id}"]  # sent as the command ended, after the events
    assert entry["event"] == "learning.feedback_captured"
    assert entry["signal"] == "gate-rejection" and entry["text"] == "Name every CSV column."
    assert entry["actor"] == "dev@example.invalid"  # an identity field: never masked
    assert entry["repo"] == "github.com/acme/app"
    assert cli("--json", "learn", "sync", run_id).json()["sent"] == {run_id: 0}  # once only


def test_a_masked_source_gets_an_id_derived_from_what_is_sent(org, workspace):
    """The control plane checks that a record's id is derived from its run, signal and
    source. When the publish scan masks part of the source (an email in a file name), the
    id is derived again from the masked source, so the run's feedback is not refused."""
    rt = Runtime(Workspace.load())
    source = "gate:design-approval:edit:docs/bob.smith@example.com.md:9b8c7d6e5f4a3b2c"
    record = FeedbackRecord(
        id=feedback_id("run-masked", "gate-edit", source),
        signal="gate-edit",
        run_id="run-masked",
        actor="dev@example.invalid",
        given_at=datetime(2026, 10, 25, 9, 0, tzinfo=UTC),
        text="Name the columns.",
        source=source,
    )
    learning.FeedbackCapture(rt, deferred=True).capture("run-masked", [record])

    (entry,) = learning.journal(rt, "run-masked").entries()
    assert "masked-email" in entry["source"]
    assert entry["feedback"] == feedback_id("run-masked", "gate-edit", entry["source"])
    assert entry["id"] == entry["feedback"]


def test_feedback_refused_now_is_sent_by_agentic_sync(org, cli):
    started = cli("--json", "run", "deliver", "--input", "request=CSV export")
    run_id = started.json()["run_id"]
    org.assignments = [{"role": "team-lead", "scope": "team", "team": "payments"}]
    org.refuse_ingest = {"status": 503, "code": "unavailable", "title": "x", "detail": "down"}
    result = cli("--json", "reject", run_id, "--feedback", "Name every CSV column.")
    assert result.code == ExitCode.PAUSED, result.err  # a decision never waits on delivery
    assert f"feedback:{run_id}" not in org.events
    org.refuse_ingest = None
    synced = cli("--json", "sync", run_id)
    assert synced.code == ExitCode.OK, synced.err
    assert synced.json()["runs"][run_id]["feedback"] == {"sent": 1}
    assert len(org.events[f"feedback:{run_id}"]) == 1


def test_attribute_in_org_mode_calls_the_endpoint(org, cli):
    done = cli(
        "--json",
        "learn",
        "attribute",
        "fb-" + "a" * 20,
        "--agent",
        "design",
        "--agent",
        "design",
        "--reason",
        "an architecture point",
    )
    assert done.code == ExitCode.OK, done.err
    assert org.corrections == [
        {"id": "fb-" + "a" * 20, "agents": ["design"], "reason": "an architecture point"}
    ]
    assert done.json()["attribution"]["method"] == "human"


def test_proposals_in_org_mode_come_from_the_control_plane(org, cli):
    org.proposals = [
        {
            "id": "lp-" + "b" * 20,
            "team": "payments",
            "agent": "developer",
            "scope": "repo:github.com/acme/app",
            "lesson": "Wrap HTTP calls in with_retry().",
            "evidence": ["fb-1", "fb-2", "fb-3"],
            "status": "pending",
            "created_at": "2026-10-25T09:00:00+00:00",
        }
    ]
    shown = cli("--json", "learn", "proposals")
    assert shown.code == ExitCode.OK, shown.err
    assert [p["id"] for p in json.loads(shown.out)] == ["lp-" + "b" * 20]
