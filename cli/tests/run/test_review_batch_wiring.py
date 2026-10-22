"""The CLI wires the diff-size budget per review batch into every run (M2-24): the
engine's budget is Zyroflo's, tightened by org policy's `review_batch`."""

from pathlib import Path

from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace
from sdlc_commons.review_batches import ReviewBatchBudget


def org_yaml(policy_repo: Path, extra: str) -> None:
    org = policy_repo / "policy" / "org.yaml"
    text = org.read_text(encoding="utf-8") if org.is_file() else ""
    org.write_text(text + extra, encoding="utf-8")


def test_a_run_gets_the_orgs_review_batch_budget(workspace, policy_repo):
    org_yaml(policy_repo, "review_batch:\n  max_files: 7\n  exempt: ['**/*.snap']\n")
    engine = Runtime(Workspace.load()).engine(offline=True)

    budget = engine.batch_budget_for("r-batch-1")
    assert isinstance(budget, ReviewBatchBudget)
    assert budget.guard.policy["max_files_per_batch"] == 7
    assert budget.guard.policy["max_lines_per_batch"] == 1500  # Zyroflo's, untightened
    assert budget.guard.policy["budget_exempt"] == ["**/*.snap"]
    assert "at most 7 files" in budget.limits()


def test_without_org_settings_the_guards_budget_applies(workspace):
    budget = Runtime(Workspace.load()).engine(offline=True).batch_budget_for("r-batch-2")
    assert budget.guard.policy["max_files_per_batch"] == 40
