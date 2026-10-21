"""Acceptance 1 (spec §1.3, M1-68): `feature-delivery` on an existing repository, in
standalone mode, from the CLI.

What runs for real: the `agentic` CLI in process (init --repo, init, run --dry-run, run,
reject, approve, edit, show, status, cost, logs, pr checks); the standalone template
install; policy, preflight, skills sync; the engine with every gate, the build loop
(review blocks round 1, QA fails round 2 on a really failing test, round 3 is clean);
per-step checkpoints on the run's own worktree branch; the native backend over HTTP with
metering and the handoff contract; guarded shell commands (pytest in the run's tree,
inside `srt` where the machine has it); the push through Zyroflo's wall to a real (bare)
remote; the pull request through the GitHub adapter.

What is stubbed: the models (a recorded cassette of a scripted upstream), GitHub's API
(`FakeGitHub`), the person merging init's pull request (a local merge), the editor (a
script), `agentic init`'s code index build, and the OS sandbox on Windows native.
"""

from decimal import Decimal

from _acceptance_support import (
    COST_USD,
    Scenario,
    approvals,
    assert_published,
    git,
    handoff,
    handoff_text,
    recorded_calls,
    sandbox_available,
    shell_results,
    success,
    tool,
    trailers_by_step,
)

from agentic_cli.exit_codes import ExitCode

SCENARIO = "m1-68"
REQUEST = "Split an invoice total evenly between several payers"
FEEDBACK = "Whole cents only: the first payers take the extra cents."

SPLIT_V1 = '''"""Splitting an amount between payers."""


def split_evenly(cents: int, ways: int) -> list[int]:
    """Split `cents` into `ways` shares that differ by at most one cent; the first
    payers take the extra cents."""
    share, extra = divmod(cents, ways)
    return [share + 1 if i < extra else share for i in range(ways)]
'''
DIVMOD = "    share, extra = divmod(cents, ways)\n"
FIRST_PAYERS = "    return [share + 1 if i < extra else share for i in range(ways)]\n"
LAST_PAYERS = "    return [share] * (ways - extra) + [share + 1] * extra\n"
GUARD = '    if ways < 1:\n        raise ValueError("ways must be at least 1")\n'
TESTS_V1 = """from invoices.split import split_evenly


def test_shares_add_up_to_the_total():
    assert sum(split_evenly(1000, 3)) == 1000


def test_remainder_goes_to_first_payers():
    assert split_evenly(1000, 3) == [334, 333, 333]
"""
TESTS_V2 = (
    "import pytest\n\n"
    + TESTS_V1
    + """

def test_zero_ways_is_refused():
    with pytest.raises(ValueError):
        split_evenly(1000, 0)
"""
)
PYTEST = "python -m pytest -q"
FAILING_TEST = "tests/test_split.py::test_remainder_goes_to_first_payers"

ITEMS = [
    {
        "id": "REQ-1",
        "statement": "An invoice total can be split evenly between several payers.",
        "acceptance_criteria": [
            "WHEN a total is split between N payers THE system SHALL return N shares that "
            "add up to the total.",
        ],
    },
    {
        "id": "REQ-2",
        "statement": "Shares are whole cents and differ by at most one cent.",
        "acceptance_criteria": [
            "WHEN a total does not divide evenly THE system SHALL give the extra cents to "
            "the first payers.",
        ],
    },
]
DESIGN_RISK = "Callers may pass zero payers."
EDITED_RISK = "Callers may pass zero payers; refuse it with ValueError."


def passing(summary: str, passed: int, criteria=("REQ-1", "REQ-2")) -> str:
    return handoff(
        "test-report",
        {"Verdict": "Pass: every test passes and every criterion holds."},
        summary=summary,
        verdict="pass",
        tests={"passed": passed, "failed": 0, "skipped": 0},
        failures=[],
        baseline_failures=[],
        acceptance_results=[
            {"criterion_id": c, "passed": True, "evidence": "tests/test_split.py"} for c in criteria
        ],
        dependency_scan={"blocked_licences": [], "vulnerabilities": []},
    )


def review(summary: str, status: str) -> str:
    return handoff(
        "review",
        {"Verdict": "See findings."},
        summary=summary,
        findings=[
            {
                "severity": "high",
                "file": "invoices/split.py",
                "line": 7,
                "issue": "split_evenly(1000, 0) raises ZeroDivisionError; refuse ways < 1 "
                "with a ValueError.",
                "status": status,
            }
        ],
        score=7.0 if status == "open" else 9.0,
        prepass_results=[],
    )


def dev_report(summary: str, files: list[str], passed: int | None) -> str:
    counts = {"passed": passed, "failed": 0, "skipped": 0} if passed is not None else None
    return handoff(
        "dev-report",
        {"What changed": summary, "Deviations from the design": "None."},
        summary=summary,
        commits=[],
        files_changed=files,
        tests_run=counts,
        deviations_from_design=[],
    )


SCRIPT = {
    "requirements": [
        tool("read_file", path="invoices/money.py"),
        handoff(
            "requirements",
            summary="Split a total evenly between payers.",
            open_questions=["When the total does not divide evenly, who pays the extra cents?"],
            size="small",
            items=ITEMS[:1],
        ),
        handoff(
            "requirements",
            {"Assumptions": "Amounts are whole cents, as in invoices.money."},
            summary="Split a total evenly; the first payers take the extra cents.",
            open_questions=[],
            size="small",
            items=ITEMS,
        ),
    ],
    "baseline": [
        tool("run_shell", command=PYTEST),
        passing("Baseline: 2 tests pass on the untouched tree.", 2, criteria=()),
    ],
    "design": [
        tool("list_dir", path="invoices"),
        handoff(
            "design",
            {"Decision outcome": "### D1: a new module invoices/split.py"},
            summary="Add invoices/split.py with split_evenly(cents, ways).",
            decisions=[
                {
                    "id": "D1",
                    "choice": "A new function split_evenly in invoices/split.py.",
                    "rationale": "Splitting is separate from totals; money.py stays small.",
                }
            ],
            impact={
                "modules": ["invoices/split.py"],
                "interfaces": ["split_evenly(cents, ways)"],
                "tests": ["tests/test_split.py"],
                "confidence": "candidate",
            },
            risks=[DESIGN_RISK],
        ),
    ],
    "develop": [
        # round 1: the feature and its tests
        tool("write_file", path="invoices/split.py", text=SPLIT_V1),
        tool("write_file", path="tests/test_split.py", text=TESTS_V1),
        tool("run_shell", command=PYTEST),
        dev_report("Added split_evenly with two tests.", ["invoices/split.py"], 4),
        # round 2: the review's finding fixed, but a "simplification" breaks REQ-2
        tool(
            "edit_file",
            path="invoices/split.py",
            old=DIVMOD + FIRST_PAYERS,
            new=GUARD + DIVMOD + LAST_PAYERS,
        ),
        tool("write_file", path="tests/test_split.py", text=TESTS_V2),
        dev_report("Refuse ways < 1; simpler share list.", ["invoices/split.py"], None),
        # round 3: QA's failure fixed
        tool("edit_file", path="invoices/split.py", old=LAST_PAYERS, new=FIRST_PAYERS),
        tool("run_shell", command=PYTEST),
        dev_report("The first payers take the extra cents again.", ["invoices/split.py"], 5),
    ],
    "review": [
        review("One blocking finding: zero payers.", "open"),
        review("The zero-payers finding is resolved.", "resolved"),
        review("Clean: nothing open.", "resolved"),
    ],
    "qa": [
        tool("run_shell", command=PYTEST),
        passing("All 4 tests pass; REQ-1 and REQ-2 hold.", 4),
        tool("run_shell", command=PYTEST),
        handoff(
            "test-report",
            {"Verdict": "Fail: the extra cents now go to the last payers (REQ-2)."},
            summary="1 test fails: the extra cents go to the last payers.",
            verdict="fail",
            tests={"passed": 4, "failed": 1, "skipped": 0},
            failures=[FAILING_TEST],
            baseline_failures=[],
            acceptance_results=[
                {"criterion_id": "REQ-1", "passed": True, "evidence": "tests/test_split.py"},
                {"criterion_id": "REQ-2", "passed": False, "evidence": FAILING_TEST},
            ],
            dependency_scan={"blocked_licences": [], "vulnerabilities": []},
        ),
        tool("run_shell", command=PYTEST),
        passing("All 5 tests pass; REQ-1 and REQ-2 hold.", 5),
    ],
}


def set_up(scenario: Scenario):
    """Init as a repo owner and as a developer would, in standalone mode."""
    repo = scenario.existing_repo()
    setup = scenario.ok("--json", "init", "--repo", "--standalone").json()
    assert setup["pull_request"]["opened"] is True, setup
    [pull] = scenario.github.pulls
    assert pull["head"]["ref"] == setup["branch"]
    scenario.merge_setup_pull_request(repo, setup["branch"])

    first = scenario.ok("--json", "init", "--standalone", code=ExitCode.ERROR).json()
    assert [c for c in first["checks"] if c["name"] == "policy: models"][0]["status"] == "missing"
    scenario.choose_models(without_sandbox=not sandbox_available())
    scenario.ok("--json", "init", "--standalone")
    return repo


def test_feature_delivery_on_an_existing_repo(scenario: Scenario):
    repo = set_up(scenario)
    main_head = git(repo, "rev-parse", "HEAD")
    scenario.models(SCRIPT)

    plan = scenario.ok("--json", "run", "feature-delivery", "--dry-run").json()
    assert plan["preflight"]["passed"], plan["preflight"]

    started = scenario.ok(
        "--json", "run", "feature-delivery", "--input", f"request={REQUEST}", code=ExitCode.PAUSED
    ).json()
    run_id = started["run_id"]
    assert started["pending"]["id"] == "requirements-approval"

    shown = scenario.ok("--json", "show", run_id).json()
    assert "who pays the extra cents" in shown["handoffs"][0]["text"]
    again = scenario.ok(
        "--json", "reject", run_id, "--feedback", FEEDBACK, code=ExitCode.PAUSED
    ).json()
    assert again["pending"]["id"] == "requirements-approval"
    at_design = scenario.ok("--json", "approve", run_id, code=ExitCode.PAUSED).json()
    assert at_design["pending"]["id"] == "design-approval"

    design = [p for p in at_design["pending"]["artefacts"] if p.startswith("handoffs/design")]
    scenario.editor(DESIGN_RISK, EDITED_RISK)
    at_review = scenario.ok(
        "--json", "edit", run_id, "--file", design[0], code=ExitCode.PAUSED
    ).json()
    assert at_review["pending"]["id"] == "review-approval", at_review

    done = scenario.ok("--json", "approve", run_id, "--notes", "ship it").json()
    assert done["status"] == "completed", done
    assert scenario.finish_models() == []  # every recorded call used, none missing

    # the steps, the loop's three rounds and the gates' decisions
    assert [(h["step"], h["iteration"], h["outcome"]) for h in done["history"]] == [
        ("requirements", 1, "completed"),  # asked who pays the extra cents
        ("requirements", 1, "completed"),  # the person's answer, after the rejection
        ("baseline", 1, "completed"),
        ("design", 1, "completed"),
        *[(s, n, "completed") for n in (1, 2, 3) for s in ("develop", "review", "qa")],
        ("publish", 1, "completed"),
    ]
    decisions = [(a["gate"], a["verdict"]) for a in approvals(repo, run_id)]
    assert decisions == [
        ("requirements-approval", "reject"),
        ("requirements-approval", "approve"),
        ("design-approval", "approve"),  # by `agentic edit`: the edited design is pinned
        ("review-approval", "approve"),
    ]
    edited = handoff_text(repo, run_id, design[0] + ".edited-1.md")
    assert EDITED_RISK in edited
    assert "verdict: fail" in handoff_text(repo, run_id, "handoffs/qa-2.md")

    # the work: three Developer checkpoints on the run's own branch, the final code
    branch = f"sdlc/{run_id}"
    developer = trailers_by_step(scenario.bare, branch)
    assert list(developer) == ["develop #3", "develop #2", "develop #1"]  # nothing else
    assert any(h.endswith(design[0] + ".edited-1.md") for h in developer["develop #1"])
    assert any(h.endswith("handoffs/review-1.md") for h in developer["develop #2"])
    assert any(h.endswith("handoffs/qa-2.md") for h in developer["develop #3"])  # QA's fail
    final = SPLIT_V1.replace(DIVMOD, GUARD + DIVMOD)
    assert git(scenario.bare, "show", f"{branch}:invoices/split.py") + "\n" == final
    assert git(scenario.bare, "show", f"{branch}:tests/test_split.py") + "\n" == TESTS_V2

    # the tests really ran in the run's tree: QA's failing round is a real failure
    assert shell_results(scenario, run_id, "baseline-1") == ["exit 0 - 2 passed"]
    assert shell_results(scenario, run_id, "qa-1") == ["exit 0 - 4 passed"]
    assert shell_results(scenario, run_id, "qa-2") == ["exit 1 - 1 failed, 4 passed"]
    assert shell_results(scenario, run_id, "develop-3") == ["exit 0 - 5 passed"]

    pull = assert_published(scenario, repo, run_id, main_head, title=REQUEST)
    for gate in ("requirements-approval", "design-approval", "review-approval"):
        assert f"- {gate}: dev@example.invalid" in pull["body"]
    assert "qa #2 `" in pull["body"]  # every handoff of every round is listed

    # the person's view afterwards: status, cost, logs, CI
    assert scenario.ok("--json", "status", run_id).json()["status"] == "completed"
    cost = scenario.ok("--json", "cost", run_id).json()
    assert Decimal(cost["total_usd"]) == Decimal(COST_USD) * recorded_calls(SCENARIO)
    assert cost["ledger_verified"] is True and Decimal(cost["unattributed_usd"]) == 0
    assert "tool run_shell: exit 1" in scenario.ok("logs", run_id).out
    scenario.github.check_runs = [[success("tests")]]
    assert scenario.ok("--json", "pr", "checks", run_id).json()["state"] == "green"
