"""Items 1+2: simulate 20 nightly bot commits of (a) the raw SQLite DB and (b) the sharded export.

Run with the venv python:  python nightly.py <repo-name> [n]
Scratch repos (all under out/scratch/<name>/):
  raw   - .agentic/index/graph.db committed as-is (+ .gitattributes binary / merge=ours)
  file  - .agentic/index/ sharded export, one JSONL per source file
  dir   - .agentic/index/ sharded export, one JSONL per source directory
"""

import gzip
import json
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from common import DB_REL, OUT, build, checkout, dir_size, git, pick_commits, rmtree, work_clone
from crg_export import export

GITATTR = ".agentic/index/graph.db binary merge=ours\n"


def init_scratch(p: Path) -> None:
    rmtree(p)
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(p)], check=True)
    for k, v in (("user.name", "bot"), ("user.email", "bot@example.invalid"), ("gc.auto", "0"),
                 ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
        git(p, "config", k, v)


def commit_all(p: Path, msg: str) -> str:
    git(p, "add", "-A")
    git(p, "commit", "-q", "--allow-empty", "-m", msg)
    return git(p, "rev-parse", "HEAD").stdout.strip()


def git_dir_size(p: Path) -> int:
    return dir_size(p / ".git")


def gc_aggressive(p: Path) -> float:
    t = time.perf_counter()
    git(p, "gc", "--aggressive", "--prune=now", "-q")
    return round(time.perf_counter() - t, 1)


def clone_sizes(p: Path, dest_base: Path) -> dict:
    out = {}
    for label, extra in (("full", []), ("depth1", ["--depth", "1"])):
        d = dest_base / label
        rmtree(d)
        subprocess.run(["git", "clone", "-q", *extra, p.resolve().as_uri(), str(d)], check=True)
        out[label] = {"total_bytes": dir_size(d), "git_bytes": git_dir_size(d)}
        rmtree(d)
    return out


def main(name: str, n: int) -> None:
    base = OUT / "scratch" / name
    rmtree(base)
    base.mkdir(parents=True)
    src = work_clone(name, "_N")
    commits = pick_commits(src, n=n, months=6)
    print(f"[{name}] {len(commits)} distinct commits", flush=True)
    repos = {k: base / k for k in ("raw", "file", "dir")}
    for k, p in repos.items():
        init_scratch(p)
    (repos["raw"] / ".gitattributes").write_text(GITATTR, encoding="utf-8", newline="\n")
    nights = []
    n1_snapshot = {}
    for idx, c in enumerate(commits):
        checkout(src, c["sha"])
        b = build(src)
        assert b["rc"] == 0, b
        dbp = src / DB_REL
        row = {"night": idx + 1, "sha": c["sha"], "date": c["date"], "build_s": b["s"], "build_peak_mb": b["peak_mb"],
               "db_bytes": dbp.stat().st_size, "wal_bytes": (dbp.with_name("graph.db-wal").stat().st_size
                                                              if dbp.with_name("graph.db-wal").exists() else 0)}
        # raw
        dst = repos["raw"] / ".agentic" / "index" / "graph.db"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dbp, dst)
        commit_all(repos["raw"], f"nightly {idx + 1} {c['sha'][:8]}")
        # sharded exports
        for mode in ("file", "dir"):
            out_dir = repos[mode] / ".agentic" / "index"
            t = time.perf_counter()
            r = export(dbp, src, out_dir, mode)
            row[f"export_{mode}_s"] = round(time.perf_counter() - t, 2)
            row[f"export_{mode}_shards"] = r["shards"]
            row[f"export_{mode}_bytes"] = dir_size(out_dir)
            assert r["root_leaks"] == 0
            commit_all(repos[mode], f"nightly {idx + 1} {c['sha'][:8]}")
            if idx > 0:
                st = git(repos[mode], "diff", "--shortstat", "HEAD~1", "HEAD").stdout.strip()
                row[f"export_{mode}_diff"] = st
        for k, p in repos.items():
            row[f"{k}_git_loose_bytes"] = git_dir_size(p)
        nights.append(row)
        print(json.dumps(row), flush=True)
        if idx == 0:  # snapshot for the "first night" baseline after gc
            for k, p in repos.items():
                snap = base / f"{k}_n1"
                shutil.copytree(p, snap)
                gc_aggressive(snap)
                n1_snapshot[k] = git_dir_size(snap)
                rmtree(snap)

    res = {"repo": name, "commits": commits, "nights": nights, "after_gc": {}}
    for k, p in repos.items():
        secs = gc_aggressive(p)
        total = git_dir_size(p)
        res["after_gc"][k] = {
            "gc_s": secs,
            "git_bytes_after_20": total,
            "git_bytes_after_1": n1_snapshot[k],
            "growth_per_night_after_first": round((total - n1_snapshot[k]) / max(1, len(commits) - 1)),
            "clones": clone_sizes(p, base / f"clone_{k}"),
        }
        print(k, json.dumps(res["after_gc"][k]), flush=True)
    # one-snapshot compressed sizes (last night)
    last_db = src / DB_REL
    with open(last_db, "rb") as f:
        res["last_db_gzip_bytes"] = len(gzip.compress(f.read(), 6))
    tar_path = base / "last_file_export.tgz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(repos["file"] / ".agentic" / "index", arcname="index")
    res["last_export_tgz_bytes"] = tar_path.stat().st_size
    tar_path.unlink()
    res["source_repo_git_bytes"] = git_dir_size(src)
    res["source_repo_worktree_bytes"] = dir_size(src, skip=(".git", ".code-review-graph"))
    (OUT / f"nightly_{name}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print("done", name, flush=True)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 20)
