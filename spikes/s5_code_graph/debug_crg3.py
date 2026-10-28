"""Debug: does crg tests_for work at symbol level on gson when given fully-qualified names?"""

import sqlite3

from code_review_graph import tools

from common import WORK, rel

root = WORK / "crg" / "gson"
con = sqlite3.connect(root / ".code-review-graph" / "graph.db")
rows = con.execute(
    "select kind, qualified_name from nodes where file_path like '%stream/JsonReader.java' and kind in ('Class','Function') limit 6"
).fetchall()
for kind, qn in rows:
    r = tools.query_graph(pattern="tests_for", target=qn, repo_root=str(root), max_results=500)
    files = sorted({rel(x.get("file_path", ""), root) for x in r.get("results", [])})
    print(kind, qn.split("::")[-1], r.get("status"), len(r.get("results", [])), files[:4])
print("TESTED_BY targets that resolve to a node:",
      con.execute("select count(*) from edges e join nodes n on n.qualified_name = e.source_qualified where e.kind='TESTED_BY'").fetchone(),
      "of", con.execute("select count(*) from edges where kind='TESTED_BY'").fetchone())
