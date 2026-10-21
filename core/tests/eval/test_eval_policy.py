"""Policy for evaluation (M1-63): the tier family override and the `eval.yaml` settings."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import EvalPolicy, PolicyError, load_policy


def copy_policy(fixtures: Path, tmp_path: Path) -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    return root


def test_eval_defaults_without_a_file(fixtures: Path, tmp_path: Path):
    policy = load_policy(copy_policy(fixtures, tmp_path))
    assert policy.eval == EvalPolicy()
    assert policy.eval.repeats == 3 and policy.eval.max_cost_increase == 0.25
    assert policy.eval.budget_for("anything") == 100


def test_eval_file_and_tier_family_load(fixtures: Path, tmp_path: Path):
    root = copy_policy(fixtures, tmp_path)
    (root / "eval.yaml").write_text(
        "eval:\n  repeats: 5\n  budget_usd: 50\n  budget_usd_by_agent: {qa: 250}\n",
        encoding="utf-8",
    )
    tiers = (root / "tiers.yaml").read_text(encoding="utf-8")
    tiers = tiers.replace(
        "  local: { model: ollama/qwen-coder }",
        "  local: { model: ollama/qwen-coder }\n  eval-judge: { model: gw/judge, family: fam-x }",
    )
    (root / "tiers.yaml").write_text(tiers, encoding="utf-8")
    policy = load_policy(root)
    assert policy.eval.repeats == 5
    assert policy.eval.budget_for("qa") == 250 and policy.eval.budget_for("ba") == 50
    assert policy.tiers["eval-judge"].family == "fam-x"


def test_unknown_eval_setting_refused(fixtures: Path, tmp_path: Path):
    root = copy_policy(fixtures, tmp_path)
    (root / "eval.yaml").write_text("eval:\n  repeat: 5\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="repeat"):
        load_policy(root)
