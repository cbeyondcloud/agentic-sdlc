"""Graphify (graphifyy 0.9.79): `graphify update` once at pinned HEAD (AST only, no LLM), replay corpus.

Run:  out/venvs/gfy/Scripts/python.exe run_gfy.py [repo ...]
Variants:
  gfy_file : graphify.affected on the seed's *file node* (what `graphify affected "<path>"` does), depth 2,
             files ordered by hit order (BFS = depth ascending)
  gfy_sym  : same reverse walk seeded from every node defined in the seed file (symbol level), merged,
             ordered by (min depth, discovery order)
"""

import subprocess
import sys
import time
import traceback
from pathlib import Path

from common import REPO_NAMES, Timer, dedupe, dir_size, load_corpus, make_work_copy, save_raw

GFY_EXE = Path(__file__).parent / "out" / "venvs" / "gfy" / "Scripts" / "graphify.exe"
SYM_CAP = 400


def norm(p: str | None) -> str | None:
    if not p:
        return None
    p = p.replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def main() -> None:
    from graphify.affected import affected_nodes, load_graph, resolve_seed

    names = sys.argv[1:] or REPO_NAMES
    for name in names:
        corpus = load_corpus(name)
        root = make_work_copy("gfy", name, corpus["head"])
        doc = {"tool": "graphify", "repo": name, "head": corpus["head"], "errors": []}
        with Timer() as t:
            p = subprocess.run(
                [str(GFY_EXE), "update", str(root)], capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
        doc["build_s"] = t.s
        doc["build_rc"] = p.returncode
        doc["build_out_tail"] = (p.stdout + p.stderr)[-800:]
        out_dir = root / "graphify-out"
        doc["index_bytes"] = dir_size(out_dir)
        doc["index_bytes_graph_json"] = (out_dir / "graph.json").stat().st_size if (out_dir / "graph.json").exists() else 0
        print(f"[gfy] {name}: build {t.s:.1f}s rc={p.returncode} size={doc['index_bytes']/1e6:.1f}MB", flush=True)
        if p.returncode != 0 or not (out_dir / "graph.json").exists():
            doc["errors"].append({"error": "build failed", "tail": doc["build_out_tail"]})
            save_raw("gfy", name, doc)
            continue
        with Timer() as tl:
            g = load_graph(out_dir / "graph.json")
        doc["graph_load_s"] = tl.s
        doc["graph_nodes"] = g.number_of_nodes()
        doc["graph_edges"] = g.number_of_edges()
        by_file: dict[str, list[str]] = {}
        for nid, d in g.nodes(data=True):
            sf = norm(d.get("source_file"))
            if sf:
                by_file.setdefault(sf, []).append(str(nid))
        queries = []
        for c in corpus["commits"]:
            seed = c["seed"]
            q = {"sha": c["sha"], "seed": seed}
            try:
                t0 = time.perf_counter()
                sid = resolve_seed(g, seed, root)
                q["seed_resolved"] = sid is not None
                files = []
                if sid is not None:
                    for h in affected_nodes(g, sid, depth=2):
                        files.append(norm(g.nodes[h.node_id].get("source_file")))
                q["file"] = dedupe([f for f in files if f], seed)
                q["t_file_s"] = time.perf_counter() - t0
                t0 = time.perf_counter()
                best: dict[str, tuple[int, int]] = {}
                order = 0
                for nid in by_file.get(seed, [])[:SYM_CAP]:
                    for h in affected_nodes(g, nid, depth=2):
                        f = norm(g.nodes[h.node_id].get("source_file"))
                        if not f or f == seed:
                            continue
                        order += 1
                        if f not in best or h.depth < best[f][0]:
                            best[f] = (h.depth, order if f not in best else best[f][1])
                q["sym"] = [f for f, _ in sorted(best.items(), key=lambda kv: kv[1])]
                q["t_sym_s"] = time.perf_counter() - t0
                q["n_seed_nodes"] = len(by_file.get(seed, []))
            except Exception as e:  # noqa: BLE001
                q["error"] = f"{type(e).__name__}: {e}"
                doc["errors"].append({"sha": c["sha"], "seed": seed, "error": q["error"], "tb": traceback.format_exc()[-800:]})
            queries.append(q)
        doc["queries"] = queries
        save_raw("gfy", name, doc)
        print(f"[gfy] {name}: done, errors={len(doc['errors'])}", flush=True)


main()
