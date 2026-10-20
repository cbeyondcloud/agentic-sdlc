"""agentic cost (M1-57): run, step, agent and child breakdown, matching the run ledger to
the cent (the ledger's step_finished events are the record)."""

import json
from decimal import Decimal

import pytest
from _cli_support import FakeGateway, answer, write

from agentic_cli import runtime
from agentic_cli.exit_codes import ExitCode

KEY = "tl-" + "fake-gateway-key-0004"  # gitleaks:allow (not a real key)

LEAD_TYPE = """  lead:
    description: Splits work across helpers.
    tier: fast
    backends: [native]
    tools: [read_files, spawn_agent]
    limits:
      max: { budget_usd: 2, max_turns: 10, max_minutes: 10 }
      children_max: { max_total: 2, max_concurrent: 1, max_depth: 1,
                      per_child: { budget_usd: 1, max_turns: 5, max_minutes: 5 } }
"""

TEAM = """name: team
version: 1.0.0
regime: team
steps:
  - id: plan
    agent: lead
  - id: requirements
    agent: ba
"""


@pytest.fixture
def gateway(workspace, policy_repo, monkeypatch) -> FakeGateway:
    types = policy_repo / "policy" / "agent-types.yaml"
    types.write_bytes(types.read_bytes() + LEAD_TYPE.encode("utf-8"))
    write(
        policy_repo / "agents" / "lead.md",
        "---\nname: lead\ntype: lead\nversion: 1.0.0\ndescription: Leads.\n---\nLead.\n",
    )
    write(policy_repo / "pipelines" / "team.yaml", TEAM)
    spawn = {"name": "helper", "instructions": "Help.", "prompt": "Check the CSV header."}
    fake = FakeGateway(
        {
            "lead": [{"tool": "spawn_agent", "args": spawn}, "planned"],
            "helper": ["header checked"],
            "ba": [answer("requirements")],
        }
    )
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    return fake


def ledger_total(workspace, run_id: str) -> Decimal:
    events = workspace / ".agentic" / "local" / "runs" / run_id / "events.jsonl"
    finished = [json.loads(line) for line in events.read_text("utf-8").splitlines()]
    return sum(
        (Decimal(e["cost"]["usd"]) for e in finished if e["event"] == "step_finished"),
        Decimal(0),
    )


def test_cost_totals_match_the_ledger_to_the_cent(workspace, gateway, cli):
    result = cli("--json", "run", "team")
    assert result.code == ExitCode.OK, result.out + result.err
    run_id = result.json()["run_id"]
    assert [r["agent"] for r in gateway.requests] == ["lead", "helper", "lead", "ba"]

    cost = cli("--json", "cost", run_id)
    assert cost.code == ExitCode.OK, cost.err
    shown = cost.json()
    call = Decimal(gateway.cost_usd)
    assert Decimal(shown["total_usd"]) == ledger_total(workspace, run_id) == 4 * call
    assert Decimal(shown["run_spent_usd"]) == Decimal(shown["total_usd"])
    assert Decimal(shown["unattributed_usd"]) == 0
    steps = {s["step"]: s for s in shown["steps"]}
    assert Decimal(steps["plan"]["usd"]) == 3 * call
    assert steps["plan"]["children"] == {"helper": str(call)}
    assert steps["plan"]["agent"] == "lead"
    assert {a: Decimal(v) for a, v in shown["by_agent"].items()} == {
        "lead": 3 * call,
        "ba": call,
    }
    assert {c: Decimal(v) for c, v in shown["by_child"].items()} == {"plan/helper": call}
    assert all(isinstance(v, str) for v in shown["by_agent"].values())  # money is text
    assert shown["ledger_verified"] is True

    human = cli("cost", run_id).out
    assert "plan" in human and "helper" in human and "$0.05" in human


def test_cost_without_a_run_lists_each_run(workspace, gateway, cli):
    run_id = cli("--json", "run", "team").json()["run_id"]
    rows = cli("--json", "cost").json()
    assert rows["runs"] == [{"run_id": run_id, "pipeline": "team", "usd": "0.0500"}]
    assert rows["total_usd"] == "0.0500"


def test_cost_of_an_unknown_run_is_an_error(workspace, cli):
    assert cli("cost", "nope").code == ExitCode.ERROR
