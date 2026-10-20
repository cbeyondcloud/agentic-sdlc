"""code-review-graph 2.3.9: build once at pinned HEAD, then replay the corpus.

Run:  PYTHONHASHSEED=0 out/venvs/crg/Scripts/python.exe run_crg.py [repo ...]
Variants recorded per commit:
  crg_impact   : tools.get_impact_radius(changed_files=[seed], depth=2).impacted_files, tool order
  crg_composed : importers_of(seed) + tests_for(seed) files first, then the remaining impact files
"""

import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path

from common import REPO_NAMES, Timer, dedupe, dir_size, load_corpus, make_work_copy, rel, save_raw

CRG_EXE = Path(__file__).parent / "out" / "venvs" / "crg" / "Scripts" / "code-review-graph.exe"


def files_from_rows(rows: list[dict], root: Path) -> list[str]:
    out = []
    for r in rows:
        p = r.get("file") or r.get("file_path") or r.get("importer")
        if p:
            out.append(rel(p, root))
    return out


def main() -> None:
    from code_review_graph import tools  # imported late so build timing is the CLI's

    names = sys.argv[1:] or REPO_NAMES
    for name in names:
        corpus = load_corpus(name)
        root = make_work_copy("crg", name, corpus["head"])
        doc = {"tool": "code-review-graph", "repo": name, "head": corpus["head"], "errors": []}
        with Timer() as t:
            p = subprocess.run(
                [str(CRG_EXE), "build", "--repo", str(root), "-q"], capture_output=True, text=True, encoding="utf-8"
            )
        doc["build_s"] = t.s
        doc["build_rc"] = p.returncode
        doc["build_stderr_tail"] = p.stderr[-600:]
        doc["index_bytes"] = dir_size(root / ".code-review-graph")
        print(f"[crg] {name}: build {t.s:.1f}s rc={p.returncode} size={doc['index_bytes']/1e6:.1f}MB", flush=True)
        queries = []
        con = sqlite3.connect(f"file:{(root / '.code-review-graph' / 'graph.db').as_posix()}?mode=ro", uri=True)
        for c in corpus["commits"]:
            seed = c["seed"]
            q = {"sha": c["sha"], "seed": seed}
            try:
                t0 = time.perf_counter()
                imp = tools.get_impact_radius(changed_files=[seed], max_depth=2, max_results=5000, repo_root=str(root))
                q["t_impact_s"] = time.perf_counter() - t0
                q["impact"] = dedupe([rel(f, root) for f in imp.get("impacted_files", [])], seed)
                q["impact_status"] = imp.get("status")
                q["impact_truncated"] = imp.get("truncated")
                t0 = time.perf_counter()
                imps = tools.query_graph(pattern="importers_of", target=seed, repo_root=str(root), max_results=500)
                q["t_importers_s"] = time.perf_counter() - t0
                q["importers"] = dedupe(files_from_rows(imps.get("results", []), root), seed)
                t0 = time.perf_counter()
                tf = tools.query_graph(pattern="tests_for", target=seed, repo_root=str(root), max_results=500)
                q["t_tests_for_s"] = time.perf_counter() - t0
                q["tests_for"] = dedupe(files_from_rows(tf.get("results", []), root), seed)
                # symbol-level tests_for: ask for every Class/Function defined in the seed file
                t0 = time.perf_counter()
                qns = [
                    r[0]
                    for r in con.execute(
                        "select qualified_name from nodes where kind in ('Class','Function') and replace(file_path, char(92), '/') like ? limit 150",
                        ("%/" + seed,),
                    )
                ]
                sym_tests: list[str] = []
                for qn in qns:
                    r = tools.query_graph(pattern="tests_for", target=qn, repo_root=str(root), max_results=200)
                    sym_tests += files_from_rows(r.get("results", []), root)
                q["t_tests_sym_s"] = time.perf_counter() - t0
                q["tests_for_sym"] = dedupe(sym_tests, seed)
                q["tests_for_all"] = dedupe(q["tests_for"] + q["tests_for_sym"], seed)
                q["composed"] = dedupe(q["importers"] + q["tests_for_all"] + q["impact"], seed)
            except Exception as e:  # noqa: BLE001
                q["error"] = f"{type(e).__name__}: {e}"
                doc["errors"].append({"sha": c["sha"], "seed": seed, "error": q["error"], "tb": traceback.format_exc()[-800:]})
            queries.append(q)
        doc["queries"] = queries
        save_raw("crg", name, doc)
        print(f"[crg] {name}: done, errors={len(doc['errors'])}", flush=True)


main()
