"""Approximate per-table payload bytes of a graph.db (dbstat is not compiled in)."""

import sqlite3
import sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
tot = {}
for t in tables:
    cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
    expr = "+".join(f'coalesce(length(cast("{c}" as blob)),0)' for c in cols)
    try:
        n, b = con.execute(f'select count(*), coalesce(sum({expr}),0) from "{t}"').fetchone()
    except sqlite3.Error as e:
        n, b = -1, -1
    tot[t] = (n, b)
for t, (n, b) in sorted(tot.items(), key=lambda kv: -kv[1][1]):
    print(f"{t:28s} rows={n:8d} payload={b/1e6:8.2f} MB")
