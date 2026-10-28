"""Acceptance 1 (spec §1.3, M1-68): `feature-delivery` on an existing repository, in
standalone mode, from the CLI.

What runs for real: the `agentic` CLI in process (init --repo, init, run --dry-run, run,
show, reject, approve, edit, status, cost, logs, pr checks); the standalone template
install; policy, preflight, skills sync; the engine with every gate and the build loop
(review blocks round 1, QA fails round 2 on a really failing test and the work goes back
to the Developer, round 3 is clean); per-step checkpoints on the run's own worktree
branch; the native backend over HTTP with metering and the handoff contract; guarded
shell commands (pytest in the run's tree, inside `srt` where the machine has it); the
push through Zyroflo's wall to a real (bare) remote; the pull request through the GitHub
adapter.

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
    handoff_text,
    recorded_calls,
    sandbox_available,
    shell_results,
    success,
    tool,
    trailers_by_step,
)
from _invoice_story import (
    DESIGN_RISK,
    DIVMOD,
    FAILING_TEST,
    FIRST_PAYERS,
    GUARD,
    ITEMS,
    LAST_PAYERS,
    PYTEST,
    REQUEST,
    SPLIT_FINAL,
    SPLIT_V1,
    TESTS_V1,
    TESTS_V2,
    design,
    dev_report,
    failing,
    passing,
    requirements,
    review,
)

from agentic_cli.exit_codes import ExitCode

SCENARIO = "m1-68"
QUESTION = "When the total does not divide evenly, who pays the extra cents?"
FEEDBACK = "Whole cents only: the first payers take the extra cents."
EDITED_RISK = "Callers may pass zero payers; refuse it with ValueError."

SCRIPT = {
    "requirements": [
        tool("read_file", path="invoices/money.py"),
        requirements(ITEMS[:1], (QUESTION,)),  # a question for the gate
        requirements(),  # the person's answer came back with the rejection
    ],
    "baseline": [
        tool("run_shell", command=PYTEST),
        passing("Baseline: 2 tests pass on the untouched tree.", 2, criteria=()),
    ],
    "design": [tool("list_dir", path="invoices"), design()],
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
        failing("the extra cents now go to the last payers.", 4, [FAILING_TEST], "REQ-2"),
        tool("run_shell", command=PYTEST),
        passing("All 5 tests pass; REQ-1 and REQ-2 hold.", 5),
    ],
}


def test_feature_delivery_on_an_existing_repo(scenario: Scenario):
    repo = scenario.existing_repo()
    scenario.init_standalone(repo)
    main_head = git(repo, "rev-parse", "HEAD")
    scenario.models(SCRIPT)

    plan = scenario.ok("--json", "run", "feature-delivery", "--dry-run").json()
    assert plan["preflight"]["passed"], plan["preflight"]
    gates = [s["id"] for s in plan["steps"] if s["kind"] == "gate"]
    assert gates == ["requirements-approval", "design-approval", "review-approval"]

    started = scenario.ok(
        "--json", "run", "feature-delivery", "--input", f"request={REQUEST}", code=ExitCode.PAUSED
    ).json()
    run_id = started["run_id"]
    assert started["pending"]["id"] == "requirements-approval"

    shown = scenario.ok("--json", "show", run_id).json()
    assert QUESTION in shown["handoffs"][0]["text"]
    again = scenario.ok(
        "--json", "reject", run_id, "--feedback", FEEDBACK, code=ExitCode.PAUSED
    ).json()
    assert again["pending"]["id"] == "requirements-approval"
    at_design = scenario.ok("--json", "approve", run_id, code=ExitCode.PAUSED).json()
    assert at_design["pending"]["id"] == "design-approval"

    design_file = [p for p in at_design["pending"]["artefacts"] if "/design" in p][0]
    scenario.editor(DESIGN_RISK, EDITED_RISK)
    at_review = scenario.ok(
        "--json", "edit", run_id, "--file", design_file, code=ExitCode.PAUSED
    ).json()
    assert at_review["pending"]["id"] == "review-approval", at_review

    done = scenario.ok("--json", "approve", run_id, "--notes", "ship it").json()
    assert done["status"] == "completed", done
    assert scenario.finish_models() == []  # every recorded call used, none missing
    # commands ran inside the OS sandbox where the machine has one (the Linux container)
    assert done["sandbox"].startswith("srt") if sandbox_available() else done["sandbox"] == "none"

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
    assert EDITED_RISK in handoff_text(repo, run_id, design_file + ".edited-1.md")
    assert "verdict: fail" in handoff_text(repo, run_id, "handoffs/qa-2.md")

    # the work: three Developer checkpoints on the run's own branch, the final code
    branch = f"sdlc/{run_id}"
    developer = trailers_by_step(scenario.bare, branch)
    assert list(developer) == ["develop #3", "develop #2", "develop #1"]  # nothing else
    assert any(h.endswith(design_file + ".edited-1.md") for h in developer["develop #1"])
    assert any(h.endswith("handoffs/review-1.md") for h in developer["develop #2"])
    assert any(h.endswith("handoffs/qa-2.md") for h in developer["develop #3"])  # QA's fail
    assert git(scenario.bare, "show", f"{branch}:invoices/split.py") + "\n" == SPLIT_FINAL
    assert git(scenario.bare, "show", f"{branch}:tests/test_split.py") + "\n" == TESTS_V2

    # the tests really ran in the run's tree: QA's failing round is a real failure
    assert shell_results(scenario, run_id, "baseline-1") == ["exit 0 - 2 passed"]
    assert shell_results(scenario, run_id, "qa-1") == ["exit 0 - 4 passed"]
    assert shell_results(scenario, run_id, "qa-2") == ["exit 1 - 1 failed, 4 passed"]
    assert shell_results(scenario, run_id, "develop-3") == ["exit 0 - 5 passed"]

    pull = assert_published(scenario, repo, run_id, main_head, title=REQUEST)
    for gate in gates:
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
