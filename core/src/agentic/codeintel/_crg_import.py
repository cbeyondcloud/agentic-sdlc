"""One index import, run in its own process (M2-44, spike S9).

    python -m agentic.codeintel._crg_import   < {"folder": ..., "tree": ..., "target": ...}

The parent has checked the export (`export.read_export`: format, tool, shard names and
sha256) and its tool version. This process imports code-review-graph, so the framework
process never does: it creates the schema through `GraphStore` in the new file `target`,
refuses a schema version other than the export's, inserts the nodes and edges with
`${ROOT}` turned back into this tree's root, restores the metadata (the export's commit
is the one an incremental `update` starts from), and runs the tool's own
post-processing - `_run_postprocess`, a private function: the public command leaves
two derived tables empty (S9). That is why the import is pinned to the tool version.
The answer is JSON on stdout: {"nodes": n, "edges": m}, or {"error": ..., "mismatch":
true|false} with exit 2 (a version mismatch) or 1.
"""

import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from agentic.codeintel.export import MANIFEST, ROOT_TOKEN


def _fail(message: str, *, mismatch: bool = False) -> None:
    sys.stdout.write(json.dumps({"error": message, "mismatch": mismatch}) + "\n")
    raise SystemExit(2 if mismatch else 1)


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"pragma table_info({table})")}


def main() -> None:
    request = json.loads(sys.stdin.read())
    from code_review_graph.build_state import BUILD_COMPLETE, BUILD_STATE_KEY
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import _canonical_repo_root
    from code_review_graph.tools.build import _run_postprocess

    folder, target = Path(request["folder"]), Path(request["target"])
    tree = _canonical_repo_root(Path(request["tree"]))
    root = tree.as_posix()
    manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    for leftover in (target, *(target.with_name(target.name + s) for s in ("-wal", "-shm"))):
        leftover.unlink(missing_ok=True)

    GraphStore(target).close()  # the schema, at this tool's version
    with closing(sqlite3.connect(target, isolation_level=None)) as con:
        found = con.execute("select value from metadata where key = 'schema_version'").fetchone()
        schema = int(found[0]) if found else 0
        if schema != manifest.get("schema_version"):
            _fail(
                f"the index export has schema version {manifest.get('schema_version')} and "
                f"this code-review-graph writes schema version {schema}; re-run the export "
                f"with this version",
                mismatch=True,
            )
        columns = {"n": _columns(con, "nodes"), "e": _columns(con, "edges")}
        now, counts = time.time(), {"n": 0, "e": 0}
        con.execute("begin")
        for rel in sorted(manifest["files"]):
            shard = folder / manifest["files"][rel]["shard"]
            # Lines end at "\n" only: str.splitlines() would also cut at U+2028, U+0085
            # and the like, which a shard keeps raw inside its strings.
            lines = shard.read_text(encoding="utf-8").split("\n")
            for number, line in enumerate(lines, 1):
                if not line:
                    continue
                where = f"the index export's shard of {rel}, line {number}"
                try:
                    record = json.loads(line)
                except ValueError as error:
                    _fail(f"{where}, is not JSON: {error}")
                if not isinstance(record, dict):
                    _fail(f"{where}, holds no record")
                kind = "n" if "qualified_name" in record else "e"
                record = {
                    key: value.replace(ROOT_TOKEN, root) if isinstance(value, str) else value
                    for key, value in record.items()
                }
                record["updated_at"] = now
                unknown = set(record) - columns[kind]
                if unknown:
                    _fail(f"the index export has columns this schema lacks: {sorted(unknown)}")
                table = "nodes" if kind == "n" else "edges"
                names = sorted(record)
                try:
                    con.execute(
                        f"insert into {table} ({', '.join(names)}) "
                        f"values ({', '.join('?' * len(names))})",
                        [record[name] for name in names],
                    )
                except sqlite3.Error as error:
                    _fail(f"{where}, was refused: {error}")
                counts[kind] += 1
        metadata = {
            **{
                key: value.replace(ROOT_TOKEN, root) if isinstance(value, str) else value
                for key, value in manifest.get("metadata", {}).items()
            },
            "git_head_sha": manifest["commit"],
            "repo_root": str(tree),
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        for key, value in sorted(metadata.items()):
            con.execute("insert or replace into metadata (key, value) values (?, ?)", (key, value))
        con.execute("commit")

    store = GraphStore(target)
    try:
        warnings = _run_postprocess(store, {}, "full", full_rebuild=True, repo_root=str(tree))
        store.set_metadata("postprocess_level", "full")
        store.set_metadata("last_postprocessed_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        store.set_metadata(BUILD_STATE_KEY, BUILD_COMPLETE)
    finally:
        store.close()
    with closing(sqlite3.connect(target)) as con:  # fold the WAL in: one file to move
        con.execute("pragma journal_mode=delete").fetchall()
    sys.stdout.write(
        json.dumps({"nodes": counts["n"], "edges": counts["e"], "warnings": warnings}) + "\n"
    )


if __name__ == "__main__":
    main()
