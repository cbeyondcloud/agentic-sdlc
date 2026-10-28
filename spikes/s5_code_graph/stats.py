"""Extra descriptive stats for the summary (reads out/results.json, corpus, raw)."""

import json
import statistics as st

from common import OUT, RAW, REPO_NAMES, load_corpus

res = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
for repo in REPO_NAMES:
    c = load_corpus(repo)
    cm = c["commits"]
    nt = [len(x["truth"]) for x in cm]
    print(repo, "head", c["head"][:10], "commits", len(cm), "mean truth", round(st.mean(nt), 2), "median", st.median(nt),
          "with tests", sum(1 for x in cm if x["truth_tests"]), "distinct seeds", len({x["seed"] for x in cm}),
          "LOC src/test", c["loc_src"], c["loc_test"], "code files", c["n_code_files"])
    ts = [x for x in cm]
    print("   newest/oldest subject:", cm[0]["subject"][:60], "|", cm[-1]["subject"][:60])
    for tool in ["crg", "gfy", "cbm"]:
        raw = json.loads((RAW / f"{tool}_{repo}.json").read_text(encoding="utf-8"))
        extra = {k: raw[k] for k in ("graph_nodes", "graph_edges", "nodes", "edges", "graph_load_s", "t_dump_s", "index_status") if k in raw}
        print("  ", tool, extra)
        if tool == "gfy":
            print("     seed_resolved:", sum(1 for q in raw["queries"] if q.get("seed_resolved")), "/", len(raw["queries"]))
# seed-not-in-index / empty analysis
for v in ("crg_impact", "gfy_file", "cbm_direct"):
    for repo in REPO_NAMES:
        e = [r for r in res["rows"] if r["variant"] == v and r["repo"] == repo and r["empty"]]
        if e:
            print(v, repo, "empty seeds:", sorted({r["seed"].split("/")[-1] for r in e}))
# paired comparison: share of commits where variant A has recall20 >= best of others
vs = sorted({r["variant"] for r in res["rows"]})
for repo in REPO_NAMES + ["ALL"]:
    print(repo, "commits where recall20==1.0:", {v: sum(1 for r in res["rows"] if r["variant"] == v and (repo == "ALL" or r["repo"] == repo) and r["recall20"] == 1.0) for v in vs})
