"""Debug: why does code-review-graph find no test files / empty results on gson?"""

import json
import sqlite3
import sys
from pathlib import Path

from common import RAW, WORK, load_corpus

repo = sys.argv[1] if len(sys.argv) > 1 else "gson"
db = WORK / "crg" / repo / ".code-review-graph" / "graph.db"
con = sqlite3.connect(db)
print("tables:", [r[0] for r in con.execute("select name from sqlite_master where type='table'")])
print("node kinds:", con.execute("select kind, count(*), sum(is_test) from nodes group by kind").fetchall())
print("edge kinds:", con.execute("select kind, count(*) from edges group by kind").fetchall())
print("languages:", con.execute("select language, count(*) from nodes where kind='File' group by language").fetchall())
print("test files:", con.execute("select count(*) from nodes where kind='File' and is_test=1").fetchall())
print("files total:", con.execute("select count(*) from nodes where kind='File'").fetchall())
raw = json.loads((RAW / f"crg_{repo}.json").read_text(encoding="utf-8"))
corpus = load_corpus(repo)
truth = {c["sha"]: c for c in corpus["commits"]}
for q in raw["queries"][:8]:
    c = truth[q["sha"]]
    print("---", q["seed"], "| nres", len(q["impact"]), "| imp", len(q["importers"]), "| tf", len(q["tests_for"]))
    print("  truth:", c["truth"][:6])
    print("  impact:", q["impact"][:6])
    n = con.execute("select count(*) from nodes where file_path like ?", ("%" + q["seed"],)).fetchone()
    print("  nodes in seed:", n)
