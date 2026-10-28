"""Dump schema + row counts + per-table byte estimate of a code-review-graph graph.db."""

import sqlite3
import sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for name, typ, sql in con.execute("select name,type,sql from sqlite_master order by type,name"):
    print(typ, name)
    if typ == "table" and not name.startswith("nodes_fts_"):
        try:
            n = con.execute(f'select count(*) from "{name}"').fetchone()[0]
            print("   rows:", n)
        except sqlite3.Error as e:
            print("   err", e)
    if sql and typ == "table":
        print("   ", sql.replace("\n", " ")[:900])
print("--- page stats")
try:
    for r in con.execute("select name, sum(pgsize) from dbstat group by name order by 2 desc limit 15"):
        print(r)
except sqlite3.Error as e:
    print("dbstat unavailable", e)
print("--- metadata")
for r in con.execute("select * from metadata"):
    print(r)
print("--- sample node")
cur = con.execute("select * from nodes limit 2")
print([d[0] for d in cur.description])
for r in cur:
    print(r)
cur = con.execute("select * from edges limit 2")
print([d[0] for d in cur.description])
for r in cur:
    print(r)
