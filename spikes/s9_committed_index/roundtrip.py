"""Item 3: can the DB be rebuilt from the export?  Run with the venv python.

Usage: python roundtrip.py <repo-name>
Steps: clone B + C at the pinned commit; fresh build in B; export B; import that export into C;
compare nodes/edges (via re-export), derived tables, and a few CLI queries.
Also: build in A (different root) and compare its export to B's (root independence).
"""

import json
import sqlite3
import sys
from pathlib import Path

from common import DB_REL, OUT, build, checkout, git, rmtree, run_crg, save_json, work_clone
from crg_export import export, import_


def con_ro(root: Path):
    c = sqlite3.connect(f"file:{(root / DB_REL).as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def derived(root: Path) -> dict:
    c = con_ro(root)
    d = {t: c.execute(f"select count(*) from {t}").fetchone()[0] for t in
         ("nodes", "edges", "flows", "flow_memberships", "communities", "risk_index", "nodes_fts", "community_summaries")}
    d["sig_nonnull"] = c.execute("select count(*) from nodes where signature is not null and signature<>''").fetchone()[0]
    rp = str(root).replace("\\", "/")
    d["node_comm"] = {
        r[0].replace(rp, "${ROOT}"): (r[1] or "").replace(rp, "${ROOT}")
        for r in c.execute("select n.qualified_name, cm.name from nodes n left join communities cm on cm.id=n.community_id")
    }
    d["sigs"] = {r[0].replace(rp, "${ROOT}"): (r[1] or "").replace(rp, "${ROOT}") for r in c.execute("select qualified_name, signature from nodes")}
    d["meta"] = {k: v for k, v in c.execute("select key,value from metadata") if k not in ("last_updated", "last_postprocessed_at", "repo_root")}
    c.close()
    return d


def shard_hashes(d: Path) -> dict:
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    return {k: v["sha256"] for k, v in m["shards"].items()}


def main(name: str) -> None:
    A = work_clone(name, "_A")
    B = work_clone(name, "_B")
    C = work_clone(name, "_C")
    head = git(A, "rev-parse", "HEAD").stdout.strip()
    res = {"repo": name, "head": head}
    for r in (A, B, C):
        checkout(r, head)
    res["build_A"] = build(A)
    res["build_B"] = build(B)
    ea, eb, ec = OUT / f"rt_{name}_eA", OUT / f"rt_{name}_eB", OUT / f"rt_{name}_eC"
    res["export_A"] = export(A / DB_REL, A, ea)
    res["export_B"] = export(B / DB_REL, B, eb)
    ha, hb = shard_hashes(ea), shard_hashes(eb)
    res["export_A_equals_B_all_shards"] = ha == hb
    res["export_A_vs_B_diff_shards"] = [k for k in set(ha) | set(hb) if ha.get(k) != hb.get(k)][:10]
    res["import_C"] = import_(eb, C)
    export(C / DB_REL, C, ec)
    hc = shard_hashes(ec)
    res["roundtrip_export_identical"] = hb == hc
    res["roundtrip_diff_shards"] = [k for k in set(hb) | set(hc) if hb.get(k) != hc.get(k)][:10]
    db, dc = derived(B), derived(C)
    res["derived_counts_B"] = {k: v for k, v in db.items() if isinstance(v, int)}
    res["derived_counts_C"] = {k: v for k, v in dc.items() if isinstance(v, int)}
    # edge 'extra' carries an ambiguous_targets list ordered by node id; ids differ after import,
    # so re-compare the shards with that list sorted to separate "ordering only" from real diffs.
    def norm(line: str) -> str:
        rec = json.loads(line)
        try:
            ex = json.loads(rec.get("extra", "{}"))
            if isinstance(ex.get("ambiguous_targets"), list):
                ex["ambiguous_targets"] = sorted(ex["ambiguous_targets"])
                rec["extra"] = json.dumps(ex, sort_keys=True)
        except (ValueError, AttributeError):
            pass
        return json.dumps(rec, sort_keys=True)

    real_diff = []
    for k in res["roundtrip_diff_shards"] if False else [k for k in set(hb) | set(hc) if hb.get(k) != hc.get(k)]:
        lb = sorted(norm(x) for x in (eb / k).read_text(encoding="utf-8").splitlines())
        lc = sorted(norm(x) for x in (ec / k).read_text(encoding="utf-8").splitlines())
        if lb != lc:
            real_diff.append(k)
    res["roundtrip_differing_shards_total"] = len([k for k in set(hb) | set(hc) if hb.get(k) != hc.get(k)])
    res["roundtrip_differing_shards_after_sorting_ambiguous_targets"] = len(real_diff)
    res["community_name_diff_nodes"] = sum(1 for k in db["node_comm"] if db["node_comm"][k] != dc["node_comm"].get(k))
    res["signatures_equal"] = db["sigs"] == dc["sigs"]
    res["node_community_equal"] = db["node_comm"] == dc["node_comm"]
    res["metadata_equal"] = db["meta"] == dc["meta"]
    res["metadata_diff"] = {k: [db["meta"].get(k), dc["meta"].get(k)] for k in set(db["meta"]) | set(dc["meta"]) if db["meta"].get(k) != dc["meta"].get(k)}
    # behaviour: status + incremental update on the imported graph at the same commit and one commit later
    res["status_C"] = run_crg(["status"], C)
    res["update_noop_C"] = run_crg(["update"], C)
    save_json(f"roundtrip_{name}.json", res)
    print(json.dumps(res, indent=1)[:6000])


if __name__ == "__main__":
    main(sys.argv[1])
