"""Debug: look at edges touching gson JsonReader in the code-review-graph DB."""

import sqlite3

from common import WORK

con = sqlite3.connect(WORK / "crg" / "gson" / ".code-review-graph" / "graph.db")
cols = [r[1] for r in con.execute("pragma table_info(edges)")]
print("edge cols", cols)
tgt = "gson/src/main/java/com/google/gson/stream/JsonReader.java"
print("--- TESTED_BY sample")
for r in con.execute("select source_qualified, target_qualified, file_path from edges where kind='TESTED_BY' limit 5"):
    print(r)
print("--- TESTED_BY touching JsonReader.java")
for r in con.execute(
    "select kind, source_qualified, target_qualified from edges where kind='TESTED_BY' and (target_qualified like ? or source_qualified like ?) limit 8",
    ("%stream/JsonReader.java%", "%stream/JsonReader.java%"),
):
    print(r)
print("--- IMPORTS_FROM from test files into stream/JsonReader")
for r in con.execute(
    "select source_qualified, target_qualified from edges where kind='IMPORTS_FROM' and target_qualified like ? limit 8", ("%JsonReader%",)
):
    print(r)
print("--- nodes named JsonReaderTest")
for r in con.execute("select kind, name, qualified_name, is_test from nodes where name like 'JsonReaderTest%' limit 6"):
    print(r)
print("--- edges from JsonReaderTest.java file")
for r in con.execute("select kind, count(*) from edges where source_qualified like ? group by kind", ("%stream/JsonReaderTest.java%",)):
    print(r)
