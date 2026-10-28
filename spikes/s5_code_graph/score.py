"""Score all raw tool outputs against the corpus; write out/results.json and out/tables.md."""

import json
import re
import statistics as st
from pathlib import Path

from common import OUT, RAW, REPO_NAMES, load_corpus

# variant -> (raw tool file prefix, ranked-list key, tests_for key, query-time keys)
VARIANTS = {
    "baseline": ("baseline", "ranked", "tests_for", ["t_s"]),
    "crg_impact": ("crg", "impact", "tests_for_all", ["t_impact_s"]),
    "crg_composed": ("crg", "composed", "tests_for_all", ["t_impact_s", "t_importers_s", "t_tests_for_s", "t_tests_sym_s"]),
    "gfy_file": ("gfy", "file", None, ["t_file_s"]),
    "gfy_sym": ("gfy", "sym", None, ["t_sym_s"]),
    "cbm_direct": ("cbm", "direct", "tests_for", ["t_direct_s"]),
    "cbm_2hop": ("cbm", "two_hop", "tests_for", ["t_direct_s"]),
}


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def main() -> None:
    rows, agg = [], {}
    meta = {}
    for repo in REPO_NAMES:
        corpus = load_corpus(repo)
        test_re = re.compile(corpus["test_re"])
        meta[repo] = {k: corpus[k] for k in ("head", "loc_src", "loc_test", "n_code_files", "scanned_commits")}
        by_sha = {c["sha"]: c for c in corpus["commits"]}
        for variant, (prefix, key, tkey, tkeys) in VARIANTS.items():
            raw = json.loads((RAW / f"{prefix}_{repo}.json").read_text(encoding="utf-8"))
            vrows = []
            for q in raw.get("queries", []):
                c = by_sha[q["sha"]]
                truth = set(c["truth"])
                truth_tests = set(c["truth_tests"])
                res = q.get(key) or []
                tf = q.get(tkey) or [] if tkey else []
                top20, top10 = res[:20], res[:10]
                r20 = len(set(top20) & truth) / len(truth)
                p10 = len(set(top10) & truth) / min(10, len(res)) if res else 0.0
                src_truth = truth - truth_tests
                row = {
                    "variant": variant,
                    "repo": repo,
                    "sha": q["sha"],
                    "seed": q["seed"],
                    "n_truth": len(truth),
                    "n_truth_tests": len(truth_tests),
                    "n_results": len(res),
                    "recall20": r20,
                    "precision10": p10,
                    "recall20_src": (len(set(top20) & src_truth) / len(src_truth)) if src_truth else None,
                    "recall20_test": (len(set(top20) & truth_tests) / len(truth_tests)) if truth_tests else None,
                    "empty": not res,
                    "test_hit_top20": bool(set(top20) & truth_tests) if truth_tests else None,
                    "test_hit_results": bool(set(res) & truth_tests) if truth_tests else None,
                    "test_hit_with_tests_for": bool((set(res) | set(tf)) & truth_tests) if truth_tests else None,
                    "n_test_files_in_results": sum(1 for f in res if test_re.search(f)),
                    "query_s": sum(q.get(k, 0.0) for k in tkeys),
                    "error": q.get("error"),
                    "top5": res[:5],
                    "truth": sorted(truth),
                }
                vrows.append(row)
            rows.extend(vrows)

            def m(field: str, vr=vrows) -> float:
                xs = [float(r[field]) for r in vr if r[field] is not None]
                return mean(xs)

            agg.setdefault(variant, {})[repo] = {
                "n_commits": len(vrows),
                "recall20": m("recall20"),
                "precision10": m("precision10"),
                "recall20_src": m("recall20_src"),
                "recall20_test": m("recall20_test"),
                "empty_share": mean([1.0 if r["empty"] else 0.0 for r in vrows]),
                "n_commits_with_test_truth": sum(1 for r in vrows if r["test_hit_results"] is not None),
                "test_hit_top20": m("test_hit_top20"),
                "test_hit_results": m("test_hit_results"),
                "test_hit_with_tests_for": m("test_hit_with_tests_for"),
                "mean_results": mean([r["n_results"] for r in vrows]),
                "mean_query_ms": 1000 * mean([r["query_s"] for r in vrows]),
                "build_s": raw.get("build_s"),
                "index_mb": (raw.get("index_bytes") or 0) / 1e6,
                "errors": len(raw.get("errors", [])) + sum(1 for r in vrows if r["error"]),
            }
    # pooled over all repos (per-commit mean)
    for variant in VARIANTS:
        vr = [r for r in rows if r["variant"] == variant]
        th = [r["test_hit_results"] for r in vr if r["test_hit_results"] is not None]
        agg[variant]["ALL"] = {
            "n_commits": len(vr),
            "recall20": mean([r["recall20"] for r in vr]),
            "precision10": mean([r["precision10"] for r in vr]),
            "empty_share": mean([1.0 if r["empty"] else 0.0 for r in vr]),
            "test_hit_results": mean([1.0 if x else 0.0 for x in th]),
            "test_hit_with_tests_for": mean([1.0 if r["test_hit_with_tests_for"] else 0.0 for r in vr if r["test_hit_with_tests_for"] is not None]),
        }
    (OUT / "results.json").write_text(json.dumps({"repos": meta, "aggregates": agg, "rows": rows}, indent=1), encoding="utf-8")

    lines = []
    for title, field, fmt in [
        ("recall@20", "recall20", "{:.3f}"),
        ("precision@10", "precision10", "{:.3f}"),
        ("empty-result share", "empty_share", "{:.0%}"),
        ("test-hit rate (any changed test in full result list)", "test_hit_results", "{:.0%}"),
        ("test-hit rate (results U tests_for answer)", "test_hit_with_tests_for", "{:.0%}"),
        ("test-hit rate (changed test within top 20)", "test_hit_top20", "{:.0%}"),
        ("recall@20 on source-file truth only", "recall20_src", "{:.3f}"),
        ("recall@20 on test-file truth only", "recall20_test", "{:.3f}"),
        ("mean results returned", "mean_results", "{:.1f}"),
        ("mean query ms", "mean_query_ms", "{:.1f}"),
        ("index build s", "build_s", "{:.1f}"),
        ("index size MB", "index_mb", "{:.1f}"),
    ]:
        lines += [f"\n**{title}**\n", "| variant | " + " | ".join(REPO_NAMES) + (" | ALL |" if "ALL" in agg["baseline"] and field in agg["baseline"]["ALL"] else " |")]
        has_all = field in agg["baseline"]["ALL"]
        lines.append("|---|" + "---|" * (len(REPO_NAMES) + (1 if has_all else 0)))
        for v in VARIANTS:
            cells = []
            for repo in REPO_NAMES + (["ALL"] if has_all else []):
                x = agg[v][repo].get(field)
                cells.append("n/a" if x is None or x != x else fmt.format(x))
            lines.append(f"| {v} | " + " | ".join(cells) + " |")
    (OUT / "tables.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


main()
