"""The engine built for a workspace (DF-47): real run ledger, Zyroflo gates, real git trees,
the native backend over a fake TokenLean, step logs through the redactor (DF-165), and
console logging that never leaks a secret (DF-175)."""

import asyncio
import io
import json
import logging
from decimal import Decimal

import pytest
from _cli_support import FakeGateway, answer

from agentic_cli import runtime
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.logsetup import console_logging
from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace

KEY = "tl-" + "fake-gateway-key-0001"  # gitleaks:allow (not a real key)


@pytest.fixture
def gateway(workspace, monkeypatch) -> FakeGateway:
    fake = FakeGateway({"ba": [answer("requirements")], "architect": [answer("design")]})
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    return fake


def test_a_run_goes_through_the_native_backend_to_its_gate(workspace, gateway):
    rt = Runtime(Workspace.load())
    state = asyncio.run(rt.engine().start("deliver", {"request": "CSV export"}, rt.user, "r-rt-1"))
    assert state.status == "awaiting_gate" and state.pending.id == "requirements-approval"
    assert [r["agent"] for r in gateway.requests] == ["ba"]
    assert state.history[-1].cost_usd == gateway.cost_usd
    assert Decimal(state.budget["spent"]) == Decimal(gateway.cost_usd)

    events = [e["event"] for e in rt.recorder("r-rt-1").events()]
    assert events[:3] == ["run_opened", "step_started", "step_finished"]
    assert events[-1] == "gate_waiting"
    log = rt.recorder("r-rt-1").run_dir / "logs" / "requirements-1.jsonl"
    assert "step started" in log.read_text("utf-8")


def test_the_gateway_key_never_lands_in_the_run_folder(workspace, gateway):
    rt = Runtime(Workspace.load())
    asyncio.run(rt.engine().start("deliver", {"request": "x"}, rt.user, "r-rt-2"))
    run_dir = rt.recorder("r-rt-2").run_dir
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text("utf-8", errors="replace"), path


def test_a_second_runtime_takes_up_the_same_run(workspace, gateway):
    first = Runtime(Workspace.load())
    asyncio.run(first.engine().start("deliver", {"request": "x"}, first.user, "r-rt-3"))
    second = Runtime(Workspace.load())
    assert second.store.load("r-rt-3").status == "awaiting_gate"
    assert second.recorder("r-rt-3").events()[0]["event"] == "run_opened"


def test_no_gateway_configured_is_refused_with_a_hint(workspace, monkeypatch):
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    with pytest.raises(CliError) as caught:
        Runtime(Workspace.load()).gateway()
    assert caught.value.code == ExitCode.REFUSED and "gateway_url" in caught.value.hint


def test_no_gateway_key_is_refused_with_the_login_hint(workspace, monkeypatch):
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("CI", "true")  # never reach for the developer's real keyring
    with pytest.raises(CliError) as caught:
        Runtime(Workspace.load()).gateway()
    assert caught.value.code == ExitCode.REFUSED
    assert "AGENTIC_TOKENLEAN_API_KEY" in str(caught.value)


def test_console_log_records_are_redacted_and_not_propagated(caplog):
    stream = io.StringIO()
    caplog.set_level(logging.DEBUG)
    with console_logging(lambda text: text.replace(KEY, "<redacted>"), stream=stream):
        logging.getLogger("agentic.engine").warning("call failed with key %s", KEY)
        logging.getLogger("agentic.engine").info("quiet")
        try:
            raise RuntimeError(f"boom {KEY}")
        except RuntimeError:
            logging.getLogger("agentic.engine").exception("step failed")
    shown = stream.getvalue()
    assert "<redacted>" in shown and KEY not in shown
    assert "quiet" not in shown  # the console shows warnings and worse
    assert not [r for r in caplog.records if r.name.startswith("agentic")]
    assert logging.getLogger("agentic").propagate  # restored afterwards


def test_preflight_reports_skills_missing_from_the_catalog(workspace, policy_repo):
    types = policy_repo / "policy" / "agent-types.yaml"
    text = types.read_text("utf-8").replace(
        "    produces: requirements\n",
        "    produces: requirements\n    skills: [acme/interviewing]\n",
    )
    types.write_text(text, encoding="utf-8", newline="\n")
    rt = Runtime(Workspace.load())
    report = rt.preflight("deliver", live=False)
    problem = next(p for p in report.problems if p.check == "skills")
    assert "acme/interviewing" in problem.detail and "catalog" in problem.detail
    assert json.dumps(rt.plan("deliver", report))  # the plan is plain JSON


def test_steps_scan_tool_output_with_the_org_policy_scanner(workspace, policy_repo):
    # batch-2 integration: the run command puts the policy scanner (DF-148) on every step's
    # tools, not the scanner's default
    org = policy_repo / "policy" / "org.yaml"
    org.write_text(org.read_text("utf-8") + "secret_scan: { action: mask }\n", "utf-8")
    rt = Runtime(Workspace.load())
    policy = rt.ws.policy()
    decision = runtime.decide_sandbox(runtime.machine_readiness(), "solo", policy.sandbox)
    wiring = rt._wiring(decision)
    assert wiring.scanner is not None and wiring.scanner.action == "mask"
