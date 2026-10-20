"""Find real merge commits whose two sides both changed source files (candidates for the merge experiment)."""

import sys

from common import S5_REPOS, git

repo = S5_REPOS / sys.argv[1]
exts = (".py",) if sys.argv[1] == "flask" else (".java",)
n = 0
for line in git(repo, "log", "--merges", "--format=%H %P", "-n", "400").stdout.splitlines():
    m, p1, p2 = line.split()[:3]
    base = git(repo, "merge-base", p1, p2).stdout.strip()
    a = [f for f in git(repo, "diff", "--name-only", base, p1).stdout.split() if f.endswith(exts)]
    b = [f for f in git(repo, "diff", "--name-only", base, p2).stdout.split() if f.endswith(exts)]
    if a and b:
        ov = sorted(set(a) & set(b))
        print(m[:10], git(repo, "log", "-1", "--format=%cs", m).stdout.strip(), "A", len(a), "B", len(b), "overlap", len(ov), ov[:3])
        n += 1
        if n >= 12:
            break
