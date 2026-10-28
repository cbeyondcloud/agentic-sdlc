import json
import statistics
import sys
from pathlib import Path

OUT = Path(__file__).parent / "out"
for name in sys.argv[1:]:
    d = json.loads((OUT / f"nightly_{name}.json").read_text(encoding="utf-8"))
    n = d["nights"]
    print(f"== {name}: {len(n)} nights, window commits {d['commits'][0].get('window_commits')}")
    print("source .git MB", d["source_repo_git_bytes"] / 1e6, "worktree MB", d["source_repo_worktree_bytes"] / 1e6)
    print("db bytes MB first/last", n[0]["db_bytes"] / 1e6, n[-1]["db_bytes"] / 1e6)
    for m in ("file", "dir"):
        print(f"export_{m} MB first/last", n[0][f"export_{m}_bytes"] / 1e6, n[-1][f"export_{m}_bytes"] / 1e6,
              "shards", n[-1][f"export_{m}_shards"], "export_s median", statistics.median(x[f"export_{m}_s"] for x in n))
    print("build_s median", statistics.median(x["build_s"] for x in n), "peak MB max", max(x["build_peak_mb"] for x in n))
    for k in ("raw", "file", "dir"):
        a = d["after_gc"][k]
        loose = n[-1][f"{k}_git_loose_bytes"]
        print(f"[{k}] loose .git after 20: {loose/1e6:.2f} MB | gc'd after 1: {a['git_bytes_after_1']/1e6:.2f} MB | gc'd after 20: {a['git_bytes_after_20']/1e6:.2f} MB"
              f" | growth/night (marginal) {a['growth_per_night_after_first']/1e3:.1f} KB | avg/night incl first {a['git_bytes_after_20']/len(n)/1e3:.1f} KB")
        c = a["clones"]
        print(f"     full clone total {c['full']['total_bytes']/1e6:.2f} MB (.git {c['full']['git_bytes']/1e6:.2f}) | depth1 total {c['depth1']['total_bytes']/1e6:.2f} MB (.git {c['depth1']['git_bytes']/1e6:.2f})")
    print("gzip(last db) MB", d["last_db_gzip_bytes"] / 1e6, "tgz(last file export) MB", d["last_export_tgz_bytes"] / 1e6)
    for m in ("file", "dir"):
        diffs = [x.get(f"export_{m}_diff") for x in n[1:]]
        print(m, "per-night diff stats:", diffs[:6], "...")
