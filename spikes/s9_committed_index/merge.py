"""Item 5: merge behaviour of a committed index, using REAL merge commits as ground truth.

For a real merge M (parents P1, P2, merge-base B): build the index at B, P1, P2, M. In scratch repos,
main = B -> P1's index, branch b2 = B -> P2's index, then `git merge b2` on main under several
.gitattributes configurations.  M's index is the "what it should have been" reference.

Run with the venv python:  python merge.py <repo> <label> <merge-sha-prefix>
"""

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from common import DB_REL, OUT, build, checkout, git, rmtree, sha256_file, work_clone
from crg_export import export

IDX = Path(".agentic") / "index"


def prep_states(name: str, label: str, msha: str) -> tuple[dict, Path]:
    src = work_clone(name, f"_M_{label}")
    msha = git(src, "rev-parse", msha).stdout.strip()
    p1, p2 = git(src, "rev-list", "--parents", "-n", "1", msha).stdout.split()[1:3]
    base = git(src, "merge-base", p1, p2).stdout.strip()
    states = {"base": base, "p1": p1, "p2": p2, "m": msha}
    root = OUT / "merge" / f"{name}_{label}"
    rmtree(root)
    for lab, sha in states.items():
        checkout(src, sha)
        build(src)
        d = root / "states" / lab
        d.mkdir(parents=True)
        shutil.copyfile(src / DB_REL, d / "graph.db")
        export(src / DB_REL, src, d / "exp")
    return states, root


def new_repo(p: Path, attrs: str | None, driver: bool) -> None:
    rmtree(p)
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(p)], check=True)
    for k, v in (("user.name", "bot"), ("user.email", "bot@example.invalid"), ("core.autocrlf", "false"),
                 ("commit.gpgsign", "false")):
        git(p, "config", k, v)
    if driver:
        git(p, "config", "merge.ours.driver", "true")
    if attrs:
        (p / ".gitattributes").write_text(attrs, encoding="utf-8", newline="\n")
    (p / "README").write_text("scratch\n")
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "init")


def put(p: Path, kind: str, state_dir: Path) -> None:
    rmtree(p / IDX)
    (p / IDX).mkdir(parents=True)
    if kind == "raw":
        shutil.copyfile(state_dir / "graph.db", p / IDX / "graph.db")
    else:
        shutil.copytree(state_dir / "exp", p / IDX, dirs_exist_ok=True)


def commit(p: Path, msg: str) -> None:
    git(p, "add", "-A")
    git(p, "commit", "-q", "--allow-empty", "-m", msg)


def lines_of(f: Path) -> list[str]:
    return f.read_text(encoding="utf-8", errors="replace").splitlines()


def run_case(root: Path, case: str, kind: str, attrs: str | None, driver: bool, only_one_side: bool = False) -> dict:
    st = root / "states"
    p = root / "repos" / case
    new_repo(p, attrs, driver)
    put(p, kind, st / "base")
    commit(p, "base")
    git(p, "checkout", "-q", "-b", "b2")
    if not only_one_side:
        put(p, kind, st / "p2")
        commit(p, "b2 index")
    else:  # the dev branch changes only source (here: README), never the index
        (p / "README").write_text("dev change\n")
        commit(p, "b2 source-only change")
    git(p, "checkout", "-q", "main")
    put(p, kind, st / "p1")
    commit(p, "main index")
    t = time.perf_counter()
    r = git(p, "merge", "--no-edit", "b2", check=False)
    out = {"case": case, "merge_rc": r.returncode, "merge_s": round(time.perf_counter() - t, 2),
           "merge_output": (r.stdout + r.stderr).strip().replace("\n", " | ")[:500]}
    status = git(p, "status", "--porcelain").stdout.splitlines()
    out["unmerged_paths"] = [s for s in status if s[:2] in ("UU", "AA", "DU", "UD", "AU", "UA")]
    out["n_unmerged"] = len(out["unmerged_paths"])
    idx = p / IDX
    if kind == "raw":
        f = idx / "graph.db"
        sha = sha256_file(f)
        shas = {k: sha256_file(st / k / "graph.db") for k in ("base", "p1", "p2", "m")}
        out["result_equals"] = [k for k, v in shas.items() if v == sha]
        head = f.read_bytes()[:16]
        out["result_is_sqlite"] = head == b"SQLite format 3\x00"
        if out["result_is_sqlite"]:
            try:
                c = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True)
                out["integrity_check"] = c.execute("pragma integrity_check").fetchone()[0]
                c.close()
            except sqlite3.Error as e:
                out["integrity_check"] = f"ERR {e}"
        else:
            out["integrity_check"] = "n/a: file is not a SQLite database (conflict markers / mangled)"
    else:
        truth = st / "m" / "exp"
        res = {"conflict_marker_files": 0, "files_equal_truth": 0, "files_differ_truth": 0, "files_missing": 0,
               "files_extra": 0, "stale_or_extra_lines": 0, "missing_lines": 0, "invalid_json_lines": 0}
        names = {str(q.relative_to(truth)).replace("\\", "/") for q in truth.rglob("*") if q.is_file()}
        got = {str(q.relative_to(idx)).replace("\\", "/") for q in idx.rglob("*") if q.is_file()}
        res["files_missing"] = len(names - got)
        res["files_extra"] = len(got - names)
        bad = []
        for n in sorted(names & got):
            a, b = (idx / n).read_bytes(), (truth / n).read_bytes()
            txt = a.decode("utf-8", "replace")
            if "<<<<<<<" in txt and ">>>>>>>" in txt:
                res["conflict_marker_files"] += 1
                bad.append(n)
            if a == b:
                res["files_equal_truth"] += 1
            else:
                res["files_differ_truth"] += 1
                ga, gb = lines_of(idx / n), lines_of(truth / n)
                sa, sb = set(ga), set(gb)
                res["stale_or_extra_lines"] += len([x for x in ga if x not in sb])
                res["missing_lines"] += len([x for x in gb if x not in sa])
                if n.endswith(".jsonl"):
                    for x in ga:
                        if x.startswith(("<<<<<<<", "=======", ">>>>>>>")):
                            continue
                        try:
                            json.loads(x)
                        except ValueError:
                            res["invalid_json_lines"] += 1
        try:
            json.loads((idx / "manifest.json").read_text(encoding="utf-8"))
            res["manifest_parses"] = True
        except ValueError:
            res["manifest_parses"] = False
        res["conflicted_examples"] = bad[:4]
        out.update(res)
    return out


def main(name: str, label: str, msha: str) -> None:
    if len(sys.argv) > 4 and sys.argv[4] == "only-one-side":  # rerun just the source-only-branch cases on saved states
        root = OUT / "merge" / f"{name}_{label}"
        for case, drv in (("raw_binary_ours_WITH_driver_dev_branch_source_only", True),
                          ("raw_binary_ours_NO_driver_dev_branch_source_only", False)):
            c = run_case(root, case, "raw", ".agentic/index/graph.db binary merge=ours\n", drv, only_one_side=True)
            print(json.dumps(c))
        c = run_case(root, "export_no_attrs_dev_branch_source_only", "exp", None, False, only_one_side=True)
        print(json.dumps(c))
        return
    states, root = prep_states(name, label, msha)
    cases = []
    # raw database
    cases.append(run_case(root, "raw_no_attrs", "raw", None, False))
    cases.append(run_case(root, "raw_binary_ours_NO_driver", "raw", ".agentic/index/graph.db binary merge=ours\n", False))
    cases.append(run_case(root, "raw_binary_ours_WITH_driver", "raw", ".agentic/index/graph.db binary merge=ours\n", True))
    cases.append(run_case(root, "raw_binary_ours_WITH_driver_only_main_changed", "raw",
                          ".agentic/index/graph.db binary merge=ours\n", True, only_one_side=True))
    cases.append(run_case(root, "raw_binary_ours_NO_driver_only_main_changed", "raw",
                          ".agentic/index/graph.db binary merge=ours\n", False, only_one_side=True))
    # text export
    cases.append(run_case(root, "export_no_attrs", "exp", None, False))
    cases.append(run_case(root, "export_merge_union", "exp", ".agentic/index/** merge=union\n", False))
    cases.append(run_case(root, "export_union_shards_manifest_ours_WITH_driver", "exp",
                          ".agentic/index/files/** merge=union\n.agentic/index/manifest.json merge=ours\n", True))
    res = {"repo": name, "label": label, "states": states, "cases": cases}
    (OUT / f"merge_{name}_{label}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    for c in cases:
        print(json.dumps({k: v for k, v in c.items() if k not in ("merge_output",)}))
        print("   ", c["merge_output"][:300])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
