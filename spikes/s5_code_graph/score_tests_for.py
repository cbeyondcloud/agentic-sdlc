"""S5 addendum (M3-27): tests_for and dependents on gson before and after the JVM
convention adapter. No tool is rebuilt and nothing calls the network.

Inputs are the recorded S5 raw answers (code-review-graph 2.3.9 and codebase-memory-mcp
v0.11.0) and a clean clone of gson at the corpus head, where `JvmConventions` answers.
"after" is what a JVM repo gets now without the binary: JvmConventions first, then
code-review-graph's answer (`Combined` order).

Usage (from the repo root):
  uv run python spikes/s5_code_graph/score_tests_for.py <S5 out folder> <scratch folder>
"""

import json
import subprocess
import sys
from pathlib import Path

from agentic.codeintel.jvm import JvmConventions


def recall(found: list[str], truth: set[str], k: int) -> float:
    return len(set(found[:k]) & truth) / len(truth)


def precision(found: list[str], truth: set[str], k: int) -> float:
    return len(set(found[:k]) & truth) / min(k, len(found)) if found else 0.0


def merged(*answers: list[str]) -> list[str]:
    return list(dict.fromkeys(f for answer in answers for f in answer))


def main(out: Path, scratch: Path) -> None:
    corpus = json.loads((out / "corpus_gson.json").read_text(encoding="utf-8"))
    tree = scratch / "gson"
    if not tree.exists():
        subprocess.run(["git", "clone", "-q", str(out / "repos" / "gson"), str(tree)], check=True)
    subprocess.run(["git", "-C", str(tree), "checkout", "-q", corpus["head"]], check=True)
    raw = {
        name: {q["sha"]: q for q in json.loads((out / "raw" / f"{name}_gson.json").read_text())["queries"]}
        for name in ("crg", "cbm", "baseline")
    }
    jvm = JvmConventions(tree)
    variants: dict[str, dict[str, list[float]]] = {}

    def score(name: str, kind: str, found: list[str], truth: set[str]) -> None:
        rows = variants.setdefault(f"{kind}: {name}", {"r20": [], "p10": [], "hit": []})
        rows["r20"].append(recall(found, truth, 20))
        rows["p10"].append(precision(found, truth, 10))
        rows["hit"].append(1.0 if set(found) & truth else 0.0)

    for commit in corpus["commits"]:
        sha, seed = commit["sha"], commit["seed"]
        crg, cbm, base = raw["crg"][sha], raw["cbm"][sha], raw["baseline"][sha]
        jvm_tests = list(jvm.tests_for(seed).files)
        jvm_deps = list(jvm.dependents(seed).files)
        tests = set(commit["truth_tests"])
        if tests:
            score("baseline (name-matched)", "tests_for", base.get("tests_for") or [], tests)
            score("code-review-graph (before)", "tests_for", crg.get("tests_for_all") or [], tests)
            score("jvm-conventions alone", "tests_for", jvm_tests, tests)
            after = merged(jvm_tests, crg.get("tests_for_all") or [])
            score("jvm-conventions + code-review-graph (after)", "tests_for", after, tests)
            score("codebase-memory-mcp v0.11.0", "tests_for", cbm.get("tests_for") or [], tests)
        truth = set(commit["truth"])
        score("code-review-graph composed (before)", "dependents", crg.get("composed") or [], truth)
        after = merged(jvm_deps, crg.get("composed") or [])
        score("jvm-conventions + code-review-graph (after)", "dependents", after, truth)
        score("codebase-memory-mcp v0.11.0 2-hop", "dependents", cbm.get("two_hop") or [], truth)

    print(f"gson @ {corpus['head'][:10]}, {len(corpus['commits'])} commits")
    print("| variant | n | recall@20 | precision@10 | hit |")
    print("|---|---|---|---|---|")
    for name, rows in variants.items():
        n = len(rows["r20"])
        mean = {key: sum(values) / n for key, values in rows.items()}
        print(f"| {name} | {n} | {mean['r20']:.2f} | {mean['p10']:.2f} | {mean['hit']:.0%} |")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
