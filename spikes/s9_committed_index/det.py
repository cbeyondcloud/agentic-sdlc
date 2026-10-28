"""Determinism: rebuild the same commit repeatedly in the same root; compare DB bytes and export bytes.

Run with the venv python:  python det.py <repo-name>
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from common import DB_REL, OUT, build, checkout, git, rmtree, sha256_file, work_clone
from crg_export import export

TS = ("updated_at", "created_at", "last_computed")


def table_fingerprints(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    out = {}
    for (t,) in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' and name not like 'nodes_fts_%'"):
        cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
        keep = [c for c in cols if c not in TS]
        raw_rows = sorted(repr(r) for r in con.execute(f'select {",".join(chr(34)+c+chr(34) for c in cols)} from "{t}"'))
        nots_rows = sorted(repr(r) for r in con.execute(f'select {",".join(chr(34)+c+chr(34) for c in keep)} from "{t}"'))
        noid_cols = [c for c in keep if c not in ("id",)]
        noid_rows = sorted(repr(r) for r in con.execute(f'select {",".join(chr(34)+c+chr(34) for c in noid_cols)} from "{t}"'))
        h = lambda rows: hashlib.sha256("\n".join(rows).encode()).hexdigest()[:12]
        out[t] = {"rows": len(raw_rows), "raw": h(raw_rows), "no_timestamps": h(nots_rows), "no_ts_no_id": h(noid_rows)}
    con.close()
    return out


def run(name: str, seeds: list) -> dict:
    root = work_clone(name, "_D")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    checkout(root, head)
    runs = []
    for i, seed in enumerate(seeds):
        env = None if seed is None else {"PYTHONHASHSEED": str(seed)}
        rmtree(root / ".code-review-graph")
        from common import run_crg

        r = run_crg(["build"], root, env=env)
        db = root / DB_REL
        ed = OUT / f"det_{name}_{i}"
        export(db, root, ed)
        import json as _j

        man = _j.loads((ed / "manifest.json").read_text(encoding="utf-8"))
        runs.append({"seed": seed, "build_s": r["s"], "db_sha": sha256_file(db), "db_bytes": db.stat().st_size,
                     "export_manifest_sha": hashlib.sha256(_j.dumps(man["shards"], sort_keys=True).encode()).hexdigest(),
                     "tables": table_fingerprints(db)})
    res = {"repo": name, "head": head, "runs": [{k: v for k, v in r.items() if k != "tables"} for r in runs]}
    res["db_byte_identical_all"] = len({r["db_sha"] for r in runs}) == 1
    res["export_identical_all"] = len({r["export_manifest_sha"] for r in runs}) == 1
    t0 = runs[0]["tables"]
    diff = {}
    for t in t0:
        for lvl in ("raw", "no_timestamps", "no_ts_no_id"):
            if len({r["tables"][t][lvl] for r in runs}) > 1:
                diff.setdefault(t, []).append(lvl)
    res["tables_that_differ_by_level"] = diff
    return res


def idle_nights(name: str, n_runs: int) -> dict:
    """Commit the DB / export from N consecutive builds of the SAME commit; report gc'd growth per idle night."""
    import shutil
    import subprocess

    from nightly import commit_all, gc_aggressive, git_dir_size, init_scratch

    root = work_clone(name, "_D")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    checkout(root, head)
    base = OUT / "scratch" / f"{name}_idle"
    rmtree(base)
    out = {}
    raw, exp = base / "raw", base / "exp"
    init_scratch(raw)
    init_scratch(exp)
    sizes = {"raw": [], "exp": []}
    for i in range(n_runs):
        build(root)
        d = raw / ".agentic" / "index"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / DB_REL, d / "graph.db")
        commit_all(raw, f"idle {i}")
        export(root / DB_REL, root, exp / ".agentic" / "index")
        commit_all(exp, f"idle {i}")
        if i == 0:
            for k, p in (("raw", raw), ("exp", exp)):
                snap = base / f"{k}_n1"
                shutil.copytree(p, snap)
                gc_aggressive(snap)
                sizes[k].append(git_dir_size(snap))
                rmtree(snap)
    for k, p in (("raw", raw), ("exp", exp)):
        gc_aggressive(p)
        sizes[k].append(git_dir_size(p))
        out[k] = {"gc_after_1": sizes[k][0], f"gc_after_{n_runs}": sizes[k][1],
                  "growth_per_idle_night": round((sizes[k][1] - sizes[k][0]) / (n_runs - 1))}
    return out


if __name__ == "__main__":
    name = sys.argv[1]
    out = {"default_random_hashseed": run(name, [None, None, None]), "hashseed_0": run(name, [0, 0]),
           "idle_nights_same_commit": idle_nights(name, 8)}
    (OUT / f"det_{name}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
