"""The feature the M1 acceptance scenarios deliver on the sample repo: split an invoice
total evenly between payers. The code the scripted Developer writes and the handoffs the
scripted agents answer with live here, shared by the scenarios that use them."""

from _acceptance_support import handoff, tool

REQUEST = "Split an invoice total evenly between several payers"
PYTEST = "python -m pytest -q"

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
SPLIT_FINAL = SPLIT_V1.replace(DIVMOD, GUARD + DIVMOD)
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


def requirements(items: list[dict] = ITEMS, questions: tuple[str, ...] = ()) -> str:
    return handoff(
        "requirements",
        {"Assumptions": "Amounts are whole cents, as in invoices.money."},
        summary="Split a total evenly; the first payers take the extra cents.",
        open_questions=list(questions),
        size="small",
        items=items,
    )


def design() -> str:
    return handoff(
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
    )


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


def failing(summary: str, passed: int, failures: list[str], criterion: str) -> str:
    return handoff(
        "test-report",
        {"Verdict": f"Fail: {summary}"},
        summary=summary,
        verdict="fail",
        tests={"passed": passed, "failed": len(failures), "skipped": 0},
        failures=failures,
        baseline_failures=[],
        acceptance_results=[
            {"criterion_id": c, "passed": c != criterion, "evidence": "tests/test_split.py"}
            for c in ("REQ-1", "REQ-2")
        ],
        dependency_scan={"blocked_licences": [], "vulnerabilities": []},
    )


def review(summary: str, status: str | None) -> str:
    """A review with one finding (`status`: open or resolved), or none (`status` None)."""
    finding = {
        "severity": "high",
        "file": "invoices/split.py",
        "line": 7,
        "issue": "split_evenly(1000, 0) raises ZeroDivisionError; refuse ways < 1 with a "
        "ValueError.",
        "status": status,
    }
    return handoff(
        "review",
        {"Verdict": "See findings."},
        summary=summary,
        findings=[finding] if status else [],
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


def one_round_script() -> dict[str, list]:
    """The feature delivered in one clean build round (no rejection, no loop-back)."""
    return {
        "requirements": [requirements()],
        "baseline": [tool("run_shell", command=PYTEST), passing("Baseline: 2 pass.", 2, ())],
        "design": [design()],
        "develop": [
            tool("write_file", path="invoices/split.py", text=SPLIT_FINAL),
            tool("write_file", path="tests/test_split.py", text=TESTS_V2),
            tool("run_shell", command=PYTEST),
            dev_report("Added split_evenly with three tests.", ["invoices/split.py"], 5),
        ],
        "review": [review("Clean: nothing found.", None)],
        "qa": [tool("run_shell", command=PYTEST), passing("All 5 tests pass.", 5)],
    }
