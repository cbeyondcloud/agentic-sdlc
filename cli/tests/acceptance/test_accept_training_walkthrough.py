"""The training walkthrough (M3-23, docs/training/walkthrough.md) on the sample repo:
init, pipelines list, a dry run, a real run that pauses at its first gate, status,
approve, the design gate, and stop. Its cassette is the one `agentic training-gateway`
serves to trainees, so the recording and what trainees get cannot drift apart.

Recorded like every acceptance cassette (`uv run python scripts/record_acceptance.py`),
from the scripted turns below: no model is called. The walkthrough stops at the design
gate, so only the Business Analyst, the test baseline and the Design agent answer.
"""

from _acceptance_support import (
    FINGERPRINT,
    Scenario,
    approvals,
    prompt_drift,
    prompt_fingerprint,
    tool,
)
from _invoice_story import PYTEST, REQUEST, design, passing, requirements

from agentic_cli.commands.training import CASSETTES, KEY
from agentic_cli.exit_codes import ExitCode

SCENARIO = "training"
CASSETTE_ROOT = CASSETTES  # the cassette lives in the CLI, for `agentic training-gateway`
assert KEY == f"{SCENARIO}/acceptance/1"

SCRIPT = {
    "requirements": [tool("read_file", path="invoices/money.py"), requirements()],
    "baseline": [
        tool("run_shell", command=PYTEST),
        passing("Baseline: the tests on the untouched tree.", 2, criteria=()),
    ],
    "design": [tool("list_dir", path="invoices"), design()],
}


def test_the_training_walkthrough(scenario: Scenario):
    repo = scenario.existing_repo()
    scenario.init_standalone(repo)
    pipelines = scenario.ok("--json", "pipelines", "list").json()
    assert "feature-delivery" in str(pipelines)
    scenario.models(SCRIPT)

    plan = scenario.ok("--json", "run", "feature-delivery", "--dry-run").json()
    assert plan["preflight"]["passed"], plan["preflight"]

    started = scenario.ok(
        "--json", "run", "feature-delivery", "--input", f"request={REQUEST}", code=ExitCode.PAUSED
    ).json()
    run_id = started["run_id"]
    assert started["pending"]["id"] == "requirements-approval"
    status = scenario.cli("--json", "status", run_id)
    assert "requirements-approval" in status.out

    at_design = scenario.ok("--json", "approve", run_id, code=ExitCode.PAUSED).json()
    assert at_design["pending"]["id"] == "design-approval"
    scenario.ok("--json", "stop", run_id, "--reason", "training walkthrough done")

    assert scenario.finish_models() == []  # every recorded answer used, none missing
    assert [(a["gate"], a["verdict"]) for a in approvals(repo, run_id)] == [
        ("requirements-approval", "approve")
    ]


def test_the_training_cassette_matches_the_current_prompts(monkeypatch):
    """As for the other cassettes (DF-316): a prompt change re-records this one too."""
    monkeypatch.setenv("AGENTIC_ACCEPTANCE_STRICT", "1")
    stamp = CASSETTES / SCENARIO / "acceptance" / FINGERPRINT
    recorded = stamp.read_text("utf-8").strip() if stamp.is_file() else None
    assert prompt_drift(recorded, prompt_fingerprint()) == []
