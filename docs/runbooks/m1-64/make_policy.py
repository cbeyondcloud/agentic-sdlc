"""Build the policy folder for the M1-64 quality-gate run (owner decisions 63-64).

    uv run python docs/runbooks/m1-64/make_policy.py

Copies the standalone policy template into ~/.agentic/eval-policy-m1-64/ (or
AGENTIC_HOME/eval-policy-m1-64), replaces its tiers and prices with the OpenRouter mapping
in this folder, adds the eval budgets, checks that the policy loads and that the judge's
family differs from the agents', and prints the folder to pass as --policy.
"""

import shutil
from pathlib import Path

from agentic.eval.judge import model_family
from agentic.policy import load_policy
from agentic.yamlio import dump_yaml, load_yaml, read_text
from agentic_cli.config import agentic_home

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parents[2] / "templates" / "standalone" / "policy"
AGENT_TIERS = ("deep-reasoning", "balanced", "fast")
JUDGE_TIER = "eval-judge"


def main() -> None:
    target = agentic_home() / "eval-policy-m1-64"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(TEMPLATE, target)
    for name in ("tiers.yaml", "prices.yaml"):
        shutil.copy(HERE / name, target / name)
    # repeats per S10 (k = 3); per-agent spend caps are passed as --budget-usd
    (target / "eval.yaml").write_text("eval:\n  repeats: 3\n", encoding="utf-8")
    # The judge may see `public` and `internal` cases (the corpus is synthetic, decision 17);
    # never `confidential` or `restricted` data.
    classes_file = target / "classifications.yaml"
    classes, _ = load_yaml(read_text(classes_file))
    for name in ("public", "internal"):
        tiers = classes["classifications"][name]["allowed_tiers"]
        if JUDGE_TIER not in tiers:
            tiers.append(JUDGE_TIER)
    classes_file.write_text(dump_yaml(classes), encoding="utf-8", newline="\n")

    policy = load_policy(target)
    families = {model_family(policy.tiers[t].model, policy.tiers[t].family) for t in AGENT_TIERS}
    judge = policy.tiers[JUDGE_TIER]
    judge_family = model_family(judge.model, judge.family)
    if judge_family in families:
        raise SystemExit(f"the judge ({judge_family}) shares a family with the agents {families}")
    internal = policy.classifications["internal"].allowed_tiers
    if JUDGE_TIER not in internal:
        raise SystemExit(f"the judge tier is not allowed for internal cases: {internal}")
    print(f"policy ready: {target}")
    print(f"  agents: {sorted(families)}   judge: {judge_family} ({judge.model})")


if __name__ == "__main__":
    main()
