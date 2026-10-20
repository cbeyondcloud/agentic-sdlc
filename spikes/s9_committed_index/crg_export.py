"""Deterministic sharded text export / import for a code-review-graph 2.3.9 graph.db.

Export: nodes + edges only (the parsed ground truth), grouped into JSONL shards by source file
(shard="file") or source directory (shard="dir"), sorted, with volatile columns (autoincrement
ids, updated_at timestamps) dropped and the absolute repo root replaced by the token ${ROOT}.
Derived tables (flows, communities, risk_index, summaries, FTS) are NOT exported: `postprocess`
regenerates them on import.

Import: create a fresh graph through GraphStore (schema + migrations), insert nodes/edges with
the root token replaced by the local root, restore non-volatile metadata, then run the tool's own
`postprocess`.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path, PurePosixPath

from common import CRG_EXE, CRG_VERSION, DB_REL, rmtree, run_crg, sha256_file

TOKEN = "${ROOT}"
VOLATILE_NODE = {"id", "updated_at", "community_id", "signature"}  # signature is derived by postprocess
VOLATILE_EDGE = {"id", "updated_at"}
VOLATILE_META = {"last_updated", "last_postprocessed_at", "repo_root"}
FORMAT = 1


def _root_variants(root: Path) -> list[str]:
    r = str(root).replace("\\", "/").rstrip("/")
    return [r]


def _swap(v, root_posix: str):
    if isinstance(v, str):
        return v.replace(root_posix, TOKEN)
    return v


def _shard_name(file_path: str, mode: str) -> str:
    # file_path already has TOKEN/ stripped to a repo-relative posix path
    p = PurePosixPath(file_path)
    if mode == "file":
        return f"files/{file_path}.jsonl"
    d = str(p.parent)
    return f"dirs/{'_root' if d == '.' else d}.jsonl"


def _line(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def export(db: Path, root: Path, out_dir: Path, mode: str = "file") -> dict:
    root_posix = _root_variants(Path(root).resolve())[0]
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ncols = [r[1] for r in con.execute("pragma table_info(nodes)") if r[1] not in VOLATILE_NODE]
    ecols = [r[1] for r in con.execute("pragma table_info(edges)") if r[1] not in VOLATILE_EDGE]
    shards: dict[str, list[tuple]] = {}
    prefix = TOKEN + "/"

    def rel(fp: str) -> str:
        fp = fp.replace(root_posix, TOKEN)
        return fp[len(prefix) :] if fp.startswith(prefix) else fp

    for row in con.execute("select * from nodes"):
        rec = {c: _swap(row[c], root_posix) for c in ncols if row[c] is not None}
        rec["t"] = "n"
        key = _shard_name(rel(row["file_path"]), mode)
        shards.setdefault(key, []).append((0, rec.get("qualified_name", ""), _line(rec)))
    for row in con.execute("select * from edges"):
        rec = {c: _swap(row[c], root_posix) for c in ecols if row[c] is not None}
        rec["t"] = "e"
        key = _shard_name(rel(row["file_path"]), mode)
        shards.setdefault(key, []).append((1, "", _line(rec)))
    meta = {k: _swap(v, root_posix) for k, v in con.execute("select key,value from metadata") if k not in VOLATILE_META}
    con.close()

    rmtree(out_dir)
    manifest_shards = {}
    leaks = 0
    for key in sorted(shards):
        lines = sorted(shards[key])  # nodes first (by qualified_name), then edges (by full json line)
        text = "".join(ln[2] + "\n" for ln in lines)
        leaks += text.count(root_posix)
        p = out_dir / key
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        manifest_shards[key] = {
            "sha256": sha256_file(p),
            "nodes": sum(1 for ln in lines if ln[0] == 0),
            "edges": sum(1 for ln in lines if ln[0] == 1),
        }
    manifest = {
        "format": FORMAT,
        "tool": "code-review-graph",
        "tool_version": CRG_VERSION,
        "shard_mode": mode,
        "root_token": TOKEN,
        "metadata": meta,
        "shards": manifest_shards,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return {"shards": len(manifest_shards), "root_leaks": leaks}


def import_(export_dir: Path, root: Path) -> dict:
    """Rebuild root/.code-review-graph/graph.db from an export. Returns timings."""
    from code_review_graph.graph import GraphStore  # only importable from the venv python

    t0 = time.perf_counter()
    root = Path(root).resolve()
    root_posix = str(root).replace("\\", "/").rstrip("/")
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tool_version"] == CRG_VERSION, "export built with a different tool version"
    dbp = root / DB_REL
    rmtree(dbp.parent)
    store = GraphStore(dbp)  # creates schema at the current version
    store.close()
    con = sqlite3.connect(str(dbp), isolation_level=None)
    con.execute("begin")
    now = time.time()
    n_nodes = n_edges = 0
    ncols = [r[1] for r in con.execute("pragma table_info(nodes)")]
    ecols = [r[1] for r in con.execute("pragma table_info(edges)")]

    def back(v):
        return v.replace(TOKEN, root_posix) if isinstance(v, str) else v

    for shard in sorted(manifest["shards"]):
        for line in (export_dir / shard).read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            t = rec.pop("t")
            rec = {k: back(v) for k, v in rec.items()}
            rec["updated_at"] = now
            tbl = "nodes" if t == "n" else "edges"
            cols = [c for c in (ncols if t == "n" else ecols) if c in rec]
            con.execute(
                f"insert into {tbl} ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                [rec[c] for c in cols],
            )
            if t == "n":
                n_nodes += 1
            else:
                n_edges += 1
    for k, v in manifest["metadata"].items():
        if k == "schema_version":
            continue
        con.execute("insert or replace into metadata(key,value) values (?,?)", (k, back(v)))
    con.execute("insert or replace into metadata(key,value) values ('repo_root', ?)", (root_posix.replace("/", "\\"),))
    con.execute("commit")
    con.close()
    t_insert = time.perf_counter() - t0
    # The public `postprocess` CLI skips the summary tables (community_summaries, risk_index);
    # `build` runs the private _run_postprocess, so call that to get an identical derived layer.
    from code_review_graph.tools.build import _run_postprocess

    t1 = time.perf_counter()
    store = GraphStore(dbp)
    warns = _run_postprocess(store, {}, "full", full_rebuild=True, repo_root=str(root))
    store.set_metadata("last_postprocessed_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    store.set_metadata("postprocess_level", "full")
    store.close()
    return {
        "nodes": n_nodes,
        "edges": n_edges,
        "insert_s": round(t_insert, 2),
        "postprocess_s": round(time.perf_counter() - t1, 2),
        "postprocess_warnings": warns,
        "total_s": round(time.perf_counter() - t0, 2),
    }


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "export":
        print(export(Path(sys.argv[2]) / DB_REL, Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else "file"))
    elif cmd == "import":
        print(import_(Path(sys.argv[2]), Path(sys.argv[3])))
