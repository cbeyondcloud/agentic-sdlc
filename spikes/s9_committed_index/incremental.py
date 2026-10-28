"""Items 3+4: local build vs incremental `update` on 5 consecutive commits, plus portability of a
committed artifact (raw DB copied to another checkout root vs export imported there).

Run with the venv python:  python incremental.py <repo-name>
"""

import json
import statistics
import sys
from pathlib import Path

from common import DB_REL, OUT, build, checkout, git, rmtree, run_crg, save_json, work_clone
from crg_export import export, import_


QFILE = {"flask": "src/flask/app.py", "gson": "gson/src/main/java/com/google/gson/Gson.java"}


def shard_map(d: Path) -> dict:
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    return {k: v["sha256"] for k, v in m["shards"].items()}


def diff_maps(a: dict, b: dict) -> int:
    return len([k for k in set(a) | set(b) if a.get(k) != b.get(k)])


def tail(r: dict) -> dict:
    return {k: r[k] for k in ("s", "rc", "peak_mb")} | {"out": (r["stdout_tail"] + r["stderr_tail"])[-250:].replace("\n", " | ")}


def main(name: str) -> None:
    I = work_clone(name, "_I")
    F = work_clone(name, "_IF")  # fresh-build reference
    shas = git(I, "rev-list", "--first-parent", "-6", "HEAD").stdout.split()[::-1]
    res = {"repo": name, "commits": shas}
    # full builds (HEAD~5 state), 3 repeats for min/median
    checkout(I, shas[0])
    full = [build(I) for _ in range(3)]
    res["full_build_s"] = [r["s"] for r in full]
    res["full_build_median_s"] = statistics.median(r["s"] for r in full)
    res["full_build_peak_mb"] = max(r["peak_mb"] for r in full)
    c0_export = OUT / f"inc_{name}_c0"
    export(I / DB_REL, I, c0_export)
    # no-op update
    res["update_noop"] = tail(run_crg(["update"], I))
    # 5 consecutive commits
    steps = []
    for i in range(1, 6):
        prev, cur = shas[i - 1], shas[i]
        files = git(I, "diff", "--name-only", prev, cur).stdout.split()
        stat = git(I, "diff", "--shortstat", prev, cur).stdout.strip()
        checkout(I, cur)
        u = run_crg(["update"], I)
        ei = OUT / f"inc_{name}_upd{i}"
        export(I / DB_REL, I, ei)
        checkout(F, cur)
        fb = build(F)
        ef = OUT / f"inc_{name}_fresh{i}"
        export(F / DB_REL, F, ef)
        steps.append({"i": i, "sha": cur[:10], "files_changed": len(files), "stat": stat, "update": tail(u),
                      "fresh_build_s": fb["s"], "fresh_build_peak_mb": fb["peak_mb"],
                      "update_vs_fresh_differing_shards": diff_maps(shard_map(ei), shard_map(ef)),
                      "total_shards": len(shard_map(ef))})
        print(json.dumps(steps[-1]), flush=True)
    res["steps"] = steps
    ups = [s["update"]["s"] for s in steps]
    res["update_median_s"] = statistics.median(ups)
    res["update_max_peak_mb"] = max(s["update"]["peak_mb"] for s in steps)

    # ---- portability: artifact produced at root I (commit c0), consumed at other roots -------------
    port = {}
    # raw db: build at c0 in I, copy to B
    checkout(I, shas[0])
    build(I)
    B = work_clone(name, "_IB")
    checkout(B, shas[0])
    (B / DB_REL).parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copyfile(I / DB_REL, B / DB_REL)
    port["raw_db_copied_to_other_root"] = {
        "status": tail(run_crg(["status"], B)),
        "importers_of_query": tail(run_crg(["query", "importers_of", QFILE[name]], B)),
    }
    checkout(B, shas[-1])
    port["raw_db_copied_to_other_root"]["update_after_checkout_of_newer_commit"] = tail(run_crg(["update"], B))
    # export imported at another root, then update to newer commit
    C = work_clone(name, "_IC")
    checkout(C, shas[0])
    ex = OUT / f"inc_{name}_port_export"
    export(I / DB_REL, I, ex)
    imp = import_(ex, C)
    checkout(C, shas[-1])
    u = run_crg(["update"], C)
    ec = OUT / f"inc_{name}_port_after_update"
    export(C / DB_REL, C, ec)
    port["export_imported_then_update"] = {
        "import": {k: imp[k] for k in ("nodes", "edges", "insert_s", "postprocess_s", "total_s")},
        "update_to_newer_commit": tail(u),
        "differs_from_fresh_build_at_that_commit_shards": diff_maps(shard_map(ec), shard_map(OUT / f"inc_{name}_fresh5")),
        "total_shards": len(shard_map(ec)),
    }
    res["portability"] = port
    save_json(f"incremental_{name}.json", res)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
