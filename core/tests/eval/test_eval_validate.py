"""The corpus validators (M1-59): every case is checked by the harness itself, so a
seeded defect outside its diff, a QA criterion the requirements do not have or a dev task
without its reference change is refused before anyone measures with it."""

from pathlib import Path

import pytest

from agentic.eval.corpus import CorpusError, load_corpus
from agentic.eval.validate import parse_hunks
from agentic.yamlio import dump_yaml

SOURCE = {"repo": "synthetic", "synthetic": True, "licence": "CC0-1.0"}

DIFF = """\
diff --git a/billing/tax.py b/billing/tax.py
--- a/billing/tax.py
+++ b/billing/tax.py
@@ -10,3 +10,5 @@ def tax(amount, rate):
     if rate < 0:
         raise ValueError(rate)
-    return amount * rate
+    if rate > 1:
+        rate = rate / 100
+    return round(amount * rate)

--- a/billing/README.md
+++ b/billing/README.md
@@ -1 +1,2 @@
 # Billing
+Rates may be given in percent.
"""

REQUIREMENTS = """\
---
kind: requirements
summary: Exports.
size: small
items:
  - id: REQ-1
    statement: Users export invoices.
    acceptance_criteria:
      - WHEN a user exports THE system SHALL queue a job.
      - WHEN the job ends THE system SHALL email a link.
  - id: REQ-2
    statement: Only finance may export.
    acceptance_criteria:
      - WHEN a non-finance user exports THE system SHALL refuse.
---
# Requirements
"""

DEV_REPORT = """\
---
kind: dev-report
summary: Done.
files_changed: [billing/export.py]
---
# Development report
"""


def write(root: Path, data: dict, folder: str) -> None:
    path = root / "cases" / folder / f"{data['id']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_yaml(data).encode("utf-8"))


def review(defects: list[dict], diff: str = DIFF) -> dict:
    return {
        "id": "r1",
        "agent": "code-review",
        "case_class": "review",
        "split": "holdout",
        "source": SOURCE,
        "inputs": {"prompt": "Review it.", "diff": diff},
        "expected": {"seeded_defects": defects},
    }


def qa(failing: list[str], requirements: str = REQUIREMENTS) -> dict:
    return {
        "id": "q1",
        "agent": "qa",
        "case_class": "qa",
        "split": "holdout",
        "source": SOURCE,
        "inputs": {
            "prompt": "Check it.",
            "handoffs": {"requirements": requirements, "dev-report": DEV_REPORT},
        },
        "expected": {"failing_criteria": failing},
    }


def defect(file: str = "billing/tax.py", lines=(12, 14)) -> dict:
    return {"file": file, "lines": list(lines), "category": "logic"}


def test_parse_hunks_gives_new_side_spans_per_file():
    assert parse_hunks(DIFF) == {"billing/tax.py": [(10, 14)], "billing/README.md": [(1, 2)]}


@pytest.mark.parametrize(
    "diff",
    [
        DIFF.replace("@@ -10,3 +10,5 @@", "@@ -10,3 +10,9 @@"),  # counts do not add up
        "--- a/x.py\n+++ b/x.py\n",  # no hunk
        "just text\n",  # not a diff
    ],
)
def test_a_malformed_diff_is_refused(tmp_path: Path, diff: str):
    write(tmp_path, review([defect()], diff=diff), "review")
    with pytest.raises(CorpusError, match="diff"):
        load_corpus(tmp_path)


def test_a_well_formed_review_case_loads(tmp_path: Path):
    write(tmp_path, review([defect(), defect("billing/README.md", (2, 2))]), "review")
    assert load_corpus(tmp_path).cases[0].truth_count == 2


@pytest.mark.parametrize(
    "bad, problem",
    [
        (defect(lines=(30, 31)), "outside the diff"),
        (defect(lines=(13, 15)), "outside the diff"),  # half in a hunk is not in it
        (defect(file="billing/other.py"), "not in the diff"),
        (defect(file="b/billing/tax.py"), "not in the diff"),
    ],
)
def test_a_defect_outside_the_diff_is_refused(tmp_path: Path, bad: dict, problem: str):
    write(tmp_path, review([defect(), bad]), "review")
    with pytest.raises(CorpusError, match=problem):
        load_corpus(tmp_path)


def test_a_review_case_needs_a_diff(tmp_path: Path):
    data = review([defect()])
    data["inputs"].pop("diff")
    write(tmp_path, data, "review")
    with pytest.raises(CorpusError, match="needs a diff"):
        load_corpus(tmp_path)


def test_a_qa_case_with_known_criteria_loads(tmp_path: Path):
    write(tmp_path, qa(["REQ-1.2"]), "qa")
    assert load_corpus(tmp_path).cases[0].truth_count == 1


@pytest.mark.parametrize("criterion", ["REQ-1.3", "REQ-9.1", "REQ-1", "req-1.1"])
def test_an_unknown_failing_criterion_is_refused(tmp_path: Path, criterion: str):
    write(tmp_path, qa([criterion]), "qa")
    with pytest.raises(CorpusError, match="not a criterion of the requirements"):
        load_corpus(tmp_path)


def test_a_qa_case_must_leave_a_criterion_passing(tmp_path: Path):
    """Otherwise failing everything scores perfectly and false alarms cannot be counted."""
    write(tmp_path, qa(["REQ-1.1", "REQ-1.2", "REQ-2.1"]), "qa")
    with pytest.raises(CorpusError, match="at least one criterion passing"):
        load_corpus(tmp_path)


def test_a_qa_case_needs_its_requirements(tmp_path: Path):
    data = qa(["REQ-1.1"])
    data["inputs"]["handoffs"].pop("requirements")
    write(tmp_path, data, "qa")
    with pytest.raises(CorpusError, match="requirements"):
        load_corpus(tmp_path)


@pytest.mark.parametrize(
    "kind, text, problem",
    [
        ("requirements", REQUIREMENTS.replace("size: small\n", ""), "size"),
        ("requirements", DEV_REPORT, "is a dev-report, not requirements"),
        ("dev-report", "no front matter at all", "dev-report"),
    ],
)
def test_an_input_handoff_must_be_valid_for_its_kind(tmp_path: Path, kind, text, problem):
    data = qa(["REQ-1.1"])
    data["inputs"]["handoffs"][kind] = text
    write(tmp_path, data, "qa")
    with pytest.raises(CorpusError, match=problem):
        load_corpus(tmp_path)


def dev_task(tmp_path: Path, **expected) -> dict:
    workspace = tmp_path / "workspaces" / "d1"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "slug.py").write_text("def slug(s):\n    return s\n", encoding="utf-8")
    (workspace / "tests" / "test_slug.py").write_text(
        "from slug import slug\n\ndef test_lower():\n    assert slug('A') == 'a'\n",
        encoding="utf-8",
    )
    solution = tmp_path / "solutions" / "d1"
    solution.mkdir(parents=True)
    (solution / "slug.py").write_text("def slug(s):\n    return s.lower()\n", encoding="utf-8")
    return {
        "id": "d1",
        "agent": "developer",
        "case_class": "dev_task",
        "split": "holdout",
        "source": SOURCE,
        "inputs": {"prompt": "Lower-case slugs.", "workspace": "workspaces/d1"},
        "expected": {
            "tests": {"fail_to_pass": ["tests/test_slug.py::test_lower"]},
            "solution": "solutions/d1",
            **expected,
        },
    }


def test_a_dev_task_with_workspace_and_solution_loads(tmp_path: Path):
    write(tmp_path, dev_task(tmp_path), "dev_task")
    case = load_corpus(tmp_path).cases[0]
    assert case.expected.solution == "solutions/d1"
    assert "solutions/d1" in case.references()


def test_a_dev_task_with_a_workspace_needs_its_solution(tmp_path: Path):
    data = dev_task(tmp_path)
    data["expected"].pop("solution")
    write(tmp_path, data, "dev_task")
    with pytest.raises(CorpusError, match="solution"):
        load_corpus(tmp_path)


def test_a_test_id_holding_space_dash_space_is_refused(tmp_path: Path):
    """DF-390: pytest's summary separates an id from its message with ' - ', so an id
    holding one would be read cut short and scored failed even when it passed."""
    data = dev_task(tmp_path)
    data["expected"]["tests"]["fail_to_pass"] = ["tests/test_slug.py::test_lower[a - b]"]
    write(tmp_path, data, "dev_task")
    with pytest.raises(CorpusError, match="' - '"):
        load_corpus(tmp_path)


def test_a_test_the_workspace_does_not_have_is_refused(tmp_path: Path):
    data = dev_task(tmp_path)
    data["expected"]["tests"]["pass_to_pass"] = ["tests/test_missing.py::test_x"]
    write(tmp_path, data, "dev_task")
    with pytest.raises(CorpusError, match="tests/test_missing.py"):
        load_corpus(tmp_path)
