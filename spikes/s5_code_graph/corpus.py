"""Build the ground-truth corpus: ~40 recent qualifying commits per repo.

Usage: python corpus.py   (stdlib only; reads out/repos/<name>, writes out/corpus_<name>.json)

Rules (see summary.md for the approximations):
  * non-merge commits, newest first, 2..15 files changed (all files, --no-renames)
  * "code files" = language extensions below (docs, lockfiles, configs ignored for truth)
  * seed = non-test code file existing at pinned HEAD with the most changed lines
  * truth = the commit's OTHER code files (source + tests) that exist at pinned HEAD
  * keep the commit only if the seed exists and truth is non-empty
"""

import json
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
REPOS = HERE / "out" / "repos"
WANT = 40

CONFIG = {
    "flask": {"exts": {".py"}, "test_re": r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$"},
    "ky": {"exts": {".ts"}, "test_re": r"(^|/)(tests?|test-d|__tests__)/|\.(test|spec)\.ts$"},
    "gson": {"exts": {".java"}, "test_re": r"(^|/)src/test/|(^|/)test/|Test\.java$|Tests\.java$"},
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout


def main() -> None:
    for name, cfg in CONFIG.items():
        repo = REPOS / name
        head = git(repo, "rev-parse", "HEAD").strip()
        tracked = set(git(repo, "ls-files").splitlines())
        test_re = re.compile(cfg["test_re"])
        is_code = lambda p: Path(p).suffix in cfg["exts"]  # noqa: E731
        out = git(repo, "log", "--no-merges", "--no-renames", "--numstat", "--format=@@%H\t%ct\t%s")
        commits, cur = [], None
        for line in out.splitlines():
            if line.startswith("@@"):
                if cur:
                    commits.append(cur)
                h, ts, subj = line[2:].split("\t", 2)
                cur = {"sha": h, "ts": int(ts), "subject": subj, "files": []}
            elif line.strip() and cur is not None:
                a, d, p = line.split("\t", 2)
                n = (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
                cur["files"].append((p, n))
        if cur:
            commits.append(cur)

        rows, scanned = [], 0
        for c in commits:
            scanned += 1
            files = c["files"]
            if not 2 <= len(files) <= 15:
                continue
            code = [(p, n) for p, n in files if is_code(p) and p in tracked]
            src = [(p, n) for p, n in code if not test_re.search(p)]
            if not src:
                continue
            seed = max(src, key=lambda x: x[1])[0]
            truth = sorted(p for p, _ in code if p != seed)
            if not truth:
                continue
            rows.append(
                {
                    "sha": c["sha"],
                    "subject": c["subject"][:100],
                    "seed": seed,
                    "truth": truth,
                    "truth_tests": [p for p in truth if test_re.search(p)],
                    "n_files_changed": len(files),
                }
            )
            if len(rows) >= WANT:
                break
        # LOC at HEAD
        loc_src = loc_test = 0
        for p in tracked:
            if is_code(p):
                try:
                    n = sum(1 for _ in (repo / p).open(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if test_re.search(p):
                    loc_test += n
                else:
                    loc_src += n
        doc = {
            "repo": name,
            "head": head,
            "scanned_commits": scanned,
            "loc_src": loc_src,
            "loc_test": loc_test,
            "n_code_files": sum(1 for p in tracked if is_code(p)),
            "test_re": cfg["test_re"],
            "commits": rows,
        }
        (HERE / "out" / f"corpus_{name}.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        n_t = sum(1 for r in rows if r["truth_tests"])
        print(f"{name}: head={head[:10]} commits={len(rows)} scanned={scanned} with_test_truth={n_t} loc_src={loc_src} loc_test={loc_test}")


main()
