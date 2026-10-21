"""One code-review-graph query, run in its own process (M1-35).

    python -m agentic.codeintel._crg_query   < {"op": ..., "tree": ..., "path"|"files": ...}

The parent sets CRG_DATA_DIR and CRG_HOME (the index in run state); this process
imports code-review-graph, answers one request and exits, so the framework process
never imports it or changes its own environment. The answer is JSON on stdout:
{"files": [...tree-relative POSIX paths, best first...], "text": "..."}.
"""

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

MAX_SYMBOLS = 150  # spike S5: tests_for for at most this many symbols of the seed file
# Result limits as spike S5 measured them (DF-90), not code-review-graph's defaults
# (100/100/500), which truncate hub files differently from the benchmark.
MAX_IMPORTERS = 500
MAX_FILE_TESTS = 500
MAX_SYMBOL_TESTS = 200
MAX_IMPACT = 5000


def _connect(db: Path) -> sqlite3.Connection:
    """The graph, read-only. The URI is built by pathlib: a `#`, `?` or `%` in the data
    folder's path would otherwise open another file (DF-90)."""
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)


def _relative(raw: str, root: Path) -> str | None:
    try:
        return Path(raw).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def _files(rows: list[dict], root: Path) -> list[str]:
    found = []
    for row in rows:
        raw = row.get("file") or row.get("file_path") or row.get("importer")
        rel = _relative(raw, root) if raw else None
        if rel:
            found.append(rel)
    return found


def _symbols(db: Path, seed: Path) -> list[str]:
    """The classes and functions defined in `seed`: rows are narrowed by file name in SQL
    (as spike S5 did), and only those paths are resolved and compared."""
    with closing(_connect(db)) as con:
        rows = con.execute(
            "select file_path, qualified_name from nodes where kind in ('Class', 'Function') "
            "and replace(file_path, char(92), '/') like ?",
            (f"%/{seed.name}",),
        ).fetchall()
    target = seed.resolve()
    names = []
    for path, name in rows:
        try:
            if Path(path).resolve() == target:
                names.append(name)
        except OSError:
            continue
    return names[:MAX_SYMBOLS]


def _dedupe(paths: list[str], *exclude: str) -> list[str]:
    seen = set(exclude)
    out = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _tests_for(tools, tree: Path, rel: str, db: Path) -> list[str]:
    root = tree.resolve()
    answer = tools.query_graph("tests_for", rel, repo_root=str(tree), max_results=MAX_FILE_TESTS)
    found = _files(answer.get("results", []), root)
    for name in _symbols(db, tree / rel):
        answer = tools.query_graph(
            "tests_for", name, repo_root=str(tree), max_results=MAX_SYMBOL_TESTS
        )
        found += _files(answer.get("results", []), root)
    return _dedupe(found, rel)


def _composed(tools, tree: Path, rel: str, db: Path) -> list[str]:
    """Importers, then tests (file and symbols), then the rest of the impact in the
    tool's own order (spike S5)."""
    root = tree.resolve()
    answer = tools.query_graph("importers_of", rel, repo_root=str(tree), max_results=MAX_IMPORTERS)
    importers = _files(answer.get("results", []), root)
    tests = _tests_for(tools, tree, rel, db)
    impact = tools.get_impact_radius(
        changed_files=[rel], max_depth=2, max_results=MAX_IMPACT, repo_root=str(tree)
    )
    impacted = [p for p in (_relative(f, root) for f in impact.get("impacted_files", [])) if p]
    return _dedupe(importers + tests + impacted, rel)


def _overview(tree: Path, db: Path) -> str:
    root = tree.resolve()
    with closing(_connect(db)) as con:
        files = con.execute("select count(distinct file_path) from nodes").fetchone()[0]
        nodes = con.execute("select count(*) from nodes").fetchone()[0]
        edges = con.execute("select count(*) from edges").fetchone()[0]
        hubs = con.execute(
            "select target_qualified, count(*) as n from edges where kind = 'IMPORTS_FROM' "
            "group by target_qualified order by n desc limit 10"
        ).fetchall()
    lines = [f"{files} files, {nodes} symbols, {edges} relations", "Most imported:"]
    for target, count in hubs:
        rel = _relative(target, root) or target
        lines.append(f"  {rel} ({count})")
    return "\n".join(lines)


def main() -> None:
    request = json.loads(sys.stdin.read())
    tree = Path(request["tree"])
    db = Path(os.environ["CRG_DATA_DIR"]) / "graph.db"
    from code_review_graph import tools

    op = request["op"]
    if op == "dependents":
        answer = {"files": _composed(tools, tree, request["path"], db)}
    elif op == "tests_for":
        answer = {"files": _tests_for(tools, tree, request["path"], db)}
    elif op == "impact":
        merged: list[str] = list(request["files"])
        for rel in request["files"]:
            merged += _composed(tools, tree, rel, db)
        answer = {"files": _dedupe(merged)}
    elif op == "overview":
        answer = {"files": [], "text": _overview(tree, db)}
    else:
        raise SystemExit(f"unknown operation {op!r}")
    sys.stdout.write(json.dumps(answer))


if __name__ == "__main__":
    main()
