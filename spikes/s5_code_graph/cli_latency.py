"""Cold CLI latency for one file-level query per tool (process start + load + query).

  python cli_latency.py win     -> code-review-graph + graphify on Windows (reads out/work/*)
  python cli_latency.py cbm     -> inside the Docker container (codebase-memory-mcp cli)
"""

import json
import statistics as st
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
N = 5


def timed(cmd: list[str], cwd: str | None = None) -> tuple[float, int, str]:
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd)
    return time.perf_counter() - t0, p.returncode, (p.stdout + p.stderr)[:120]


def bench(label: str, cmd: list[str], cwd: str | None = None) -> dict:
    ts = []
    rc = 0
    for _ in range(N):
        t, rc, _o = timed(cmd, cwd)
        ts.append(t)
    r = {"label": label, "median_s": st.median(ts), "min_s": min(ts), "rc": rc}
    print(json.dumps(r), flush=True)
    return r


def main() -> None:
    mode = sys.argv[1]
    res = []
    if mode == "win":
        out = HERE / "out"
        seed = "gson/src/main/java/com/google/gson/stream/JsonReader.java"
        crg = str(out / "venvs/crg/Scripts/code-review-graph.exe")
        gfy = str(out / "venvs/gfy/Scripts/graphify.exe")
        r = str(out / "work/crg/gson")
        res.append(bench("crg impact CLI (gson, JsonReader.java)", [crg, "impact", "--files", seed, "--repo", r]))
        res.append(bench("crg query importers_of CLI", [crg, "query", "importers_of", seed, "--repo", r]))
        g = str(out / "work/gfy/gson/graphify-out/graph.json")
        res.append(bench("graphify affected CLI (gson, JsonReader.java)", [gfy, "affected", seed, "--graph", g]))
    else:
        seed = "gson/src/main/java/com/google/gson/stream/JsonReader.java"
        q = f"MATCH (a)-[r]->(b) WHERE b.file_path = '{seed}' RETURN a.file_path, type(r), count(*)"
        res.append(bench("cbm cli query_graph (in container)", ["codebase-memory-mcp", "cli", "query_graph", "--project", "work-gson", "--format", "json", "--query", q]))
    base = HERE / "out" if mode == "win" else Path("/out")
    (base / f"cli_latency_{mode}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")


main()
