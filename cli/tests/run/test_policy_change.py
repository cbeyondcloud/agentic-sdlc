"""A standalone or `--policy` run resumed after its owner edited the policy (DF-697 remainder,
owner decision 105, 2026-10-28): the run continues, and its ledger records the change - the
first event after the resume carries the version in force before (`policy_from`) and the
new one (`policy_to`), and its step says where. A resume without an edit records nothing.
Org policies (control plane or git) are still refused: test_org_preflight.py."""

import json
from pathlib import Path

import pytest
from _cli_support import FakeGateway, answer

from agentic_cli import runtime
from agentic_cli.exit_codes import ExitCode
from sdlc_commons.ledger import RunLedger

KEY = "tl-" + "fake-gateway-key-0697"  # gitleaks:allow (not a real key)


@pytest.fixture
def gateway(workspace, monkeypatch) -> FakeGateway:
    fake = FakeGateway(
        {
            "ba": [answer("requirements"), answer("requirements", summary="Second try.")] * 2,
            "architect": [answer("design")],
        }
    )
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    return fake


def start(cli) -> tuple[str, str]:
    result = cli("--json", "run", "deliver", "--input", "request=CSV export")
    assert result.code == ExitCode.PAUSED, result.err
    return result.json()["run_id"], result.json()["policy_version"]


def edit_policy(policy_repo: Path, note: str) -> None:
    """The owner edits their own policy repo: its version (a content digest) moves."""
    org_yaml = policy_repo / "policy" / "org.yaml"
    org_yaml.write_text(org_yaml.read_text(encoding="utf-8") + f"# {note}\n", "utf-8")


def changes(workspace: Path, run_id: str) -> list[tuple[str, str, str, str]]:
    """(event, step, policy_from, policy_to) of each event that records a policy change."""
    path = workspace / ".agentic" / "local" / "runs" / run_id / "events.jsonl"
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        block = (event.get("ext") or {}).get("agentic") or {}
        if "policy_from" in block:
            step = event.get("step", "")
            found.append((event["event"], step, block["policy_from"], block["policy_to"]))
    return found


def test_a_resumed_standalone_run_continues_and_its_ledger_records_the_change(
    workspace, policy_repo, gateway, cli
):
    run_id, started_on = start(cli)
    edit_policy(policy_repo, "the owner tightened a limit")
    approved = cli("--json", "approve", run_id)
    assert approved.code == ExitCode.OK, approved.err  # it continued
    assert [r["agent"] for r in gateway.requests] == ["ba", "architect"]
    [(event, step, old, new)] = changes(workspace, run_id)
    assert old == started_on and new != started_on and new.startswith("sha256:")
    assert step  # where the new version took effect
    assert started_on in approved.err and new in approved.err  # the person is told
    assert RunLedger(workspace, run_id).verify() == []


def test_the_change_is_recorded_once_per_edit(workspace, policy_repo, gateway, cli):
    """The version in force is the last one the ledger recorded: a resume without an edit
    adds nothing, a second edit records a change from the first edit's version."""
    run_id, started_on = start(cli)
    edit_policy(policy_repo, "first edit")
    first = cli("--json", "reject", run_id, "--feedback", "cover the CSV header")
    assert first.code == ExitCode.PAUSED, first.err
    [(_, _, old, after_first)] = changes(workspace, run_id)
    assert old == started_on
    again = cli("--json", "reject", run_id, "--feedback", "and the footer")
    assert again.code == ExitCode.PAUSED, again.err
    assert len(changes(workspace, run_id)) == 1  # no edit: nothing new
    edit_policy(policy_repo, "second edit")
    done = cli("--json", "approve", run_id)
    assert done.code == ExitCode.OK, done.err
    recorded = changes(workspace, run_id)
    assert [(c[2], c[3]) for c in recorded][1] == (after_first, recorded[1][3])
    assert recorded[1][3] not in (started_on, after_first)
    assert RunLedger(workspace, run_id).verify() == []
