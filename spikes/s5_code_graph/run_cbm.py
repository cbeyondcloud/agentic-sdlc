"""codebase-memory-mcp v0.11.0 inside Docker (Linux, --network none). Runs in the container with python 3.12.

Mounts:  /spike (this dir, ro)   /out (spikes/s5_code_graph/out, rw)   volume s5cbm_data -> /data (HOME)
Usage (in container):  python /spike/run_cbm.py [repo ...]

Talks to the binary over MCP stdio (JSON-RPC) for queries; index via the same server.
Variants:
  cbm_direct : files owning nodes with a code edge (IMPORTS/CALLS/USAGE/INHERITS/TESTS/...) into the seed's
               nodes (1 hop), ranked by edge count desc
  cbm_2hop   : direct, then files that have a code edge into any direct dependent (from one bulk dump)
  tests_for  : sources of TESTS edges into the seed's nodes
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path("/out")
BIN = "codebase-memory-mcp"
NOISE = {
    "DEFINES", "DEFINES_METHOD", "CONTAINS_FILE", "CONTAINS_FOLDER", "CONTAINS_PACKAGE", "SEMANTICALLY_RELATED",
    "SIMILAR_TO", "FILE_CHANGES_WITH", "HAS_BRANCH", "MEMBER_OF",
}  # structural / similarity / git-history-derived edges are not dependencies


class MCP:
    def __init__(self) -> None:
        self.p = subprocess.Popen([BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "s5", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, m: dict) -> None:
        self.p.stdin.write(json.dumps(m) + "\n")
        self.p.stdin.flush()

    def rpc(self, method: str, params: dict):
        self.n += 1
        i = self.n
        self.send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server closed")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == i:
                if "error" in msg:
                    raise RuntimeError(json.dumps(msg["error"])[:300])
                return msg["result"]

    def tool(self, name: str, args: dict):
        res = self.rpc("tools/call", {"name": name, "arguments": args})
        text = "".join(c.get("text", "") for c in res.get("content", []))
        if res.get("isError"):
            raise RuntimeError(text[:300])
        return json.loads(text)

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.p.kill()


def du(path: str) -> int:
    t = 0
    for r, _d, fs in os.walk(path):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return t


def cypher(m: MCP, project: str, q: str) -> list[list]:
    r = m.tool("query_graph", {"project": project, "query": q, "max_rows": 99998, "format": "json"})
    return r.get("rows", [])


def main() -> None:
    names = sys.argv[1:] or ["flask", "ky", "gson"]
    cache = "/data/.cache/codebase-memory-mcp"
    for name in names:
        corpus = json.loads((OUT / f"corpus_{name}.json").read_text(encoding="utf-8"))
        test_re = re.compile(corpus["test_re"])
        src = Path("/work") / name
        if src.exists():
            shutil.rmtree(src)
        shutil.copytree(OUT / "repos" / name, src)
        doc = {"tool": "codebase-memory-mcp", "repo": name, "head": corpus["head"], "errors": [], "queries": []}
        before = du(cache) if os.path.exists(cache) else 0
        m = MCP()
        try:
            t0 = time.perf_counter()
            r = m.tool("index_repository", {"repo_path": str(src)})
            doc["build_s"] = time.perf_counter() - t0
            project = r["project"]
            doc["project"] = project
            doc["index_status"] = r.get("status")
            doc["nodes"], doc["edges"] = r.get("nodes"), r.get("edges")
            m.close()
            m = MCP()  # fresh server process, index persisted on disk
            doc["index_bytes"] = du(cache) - before
            print(f"[cbm] {name}: index {doc['build_s']:.1f}s status={doc['index_status']} nodes={doc['nodes']} edges={doc['edges']} size={doc['index_bytes']/1e6:.1f}MB", flush=True)
            # bulk file->file dump for the 2-hop variant (one query)
            t0 = time.perf_counter()
            rows = cypher(m, project, "MATCH (a)-[r]->(b) RETURN a.file_path, b.file_path, type(r), count(*)")
            doc["t_dump_s"] = time.perf_counter() - t0
            rev: dict[str, Counter] = defaultdict(Counter)  # target file -> {source file: count}
            for a, b, ty, n in rows:
                if not a or not b or a == b or ty in NOISE:
                    continue
                rev[b][a] += int(n)
            for c in corpus["commits"]:
                seed = c["seed"]
                q = {"sha": c["sha"], "seed": seed}
                try:
                    esc = seed.replace("'", "\\'")
                    t0 = time.perf_counter()
                    rows = cypher(
                        m, project,
                        f"MATCH (a)-[r]->(b) WHERE b.file_path = '{esc}' RETURN a.file_path, type(r), count(*)",
                    )
                    q["t_direct_s"] = time.perf_counter() - t0
                    cnt: Counter = Counter()
                    tests: Counter = Counter()
                    for a, ty, n in rows:
                        if not a or a == seed or ty in NOISE:
                            continue
                        cnt[a] += int(n)
                        if ty == "TESTS":
                            tests[a] += int(n)
                    q["direct"] = [f for f, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))]
                    q["tests_for"] = [f for f, _ in sorted(tests.items(), key=lambda kv: (-kv[1], kv[0]))]
                    seen = set(q["direct"]) | {seed}
                    hop2: Counter = Counter()
                    for d in q["direct"]:
                        for f, n in rev.get(d, {}).items():
                            if f not in seen:
                                hop2[f] += n
                    q["two_hop"] = q["direct"] + [f for f, _ in sorted(hop2.items(), key=lambda kv: (-kv[1], kv[0]))]
                except Exception as e:  # noqa: BLE001
                    q["error"] = f"{type(e).__name__}: {e}"
                    doc["errors"].append({"sha": c["sha"], "seed": seed, "error": q["error"]})
                doc["queries"].append(q)
        except Exception as e:  # noqa: BLE001
            doc["errors"].append({"error": f"{type(e).__name__}: {e}"})
        finally:
            m.close()
        (OUT / "raw").mkdir(exist_ok=True)
        (OUT / "raw" / f"cbm_{name}.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        print(f"[cbm] {name}: done errors={len(doc['errors'])}", flush=True)


main()
