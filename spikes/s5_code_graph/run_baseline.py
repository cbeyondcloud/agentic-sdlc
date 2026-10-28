"""Baseline: naive reverse-import scan (no graph, no index, stdlib only).

dependents(seed) = files whose import statements reference the seed module
                 + test files whose name matches the seed (tests_for)
Ranking: name-matching tests first, then importers by closeness to the seed's directory, then path.
Java: same-package files have no import statement, so same-package files that mention the class
name as a word are also counted (otherwise the baseline would be a straw man).
"""

import ast
import posixpath
import re
import sys
import time

from common import REPO_NAMES, REPOS, Timer, dedupe, git, load_corpus, save_raw

EXTS = {"flask": (".py",), "ky": (".ts",), "gson": (".java",)}


def read(name: str, p: str) -> str:
    try:
        return (REPOS / name / p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------- Python ----------
def py_module_names(path: str) -> list[str]:
    """Dotted module names a file may be imported as (with and without src/ prefix)."""
    stem = path[:-3]
    parts = stem.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    names = {".".join(parts)}
    if parts and parts[0] == "src":
        names.add(".".join(parts[1:]))
    return [n for n in names if n]


def py_imports(path: str, text: str) -> set[str]:
    """Candidate imported dotted module names (absolute), resolving relative imports."""
    out: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    pkg_parts = path.split("/")[:-1]
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name)
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = pkg_parts[: len(pkg_parts) - (n.level - 1)]
                if base and base[0] == "src":
                    base = base[1:]
                mod = ".".join(base + ([n.module] if n.module else []))
            else:
                mod = n.module or ""
            if mod:
                out.add(mod)
                for a in n.names:
                    out.add(f"{mod}.{a.name}")  # `from pkg import submodule`
    return out


# ---------- TypeScript ----------
TS_IMPORT = re.compile(r"""(?:import|export)\s[^'"`]*?from\s*['"]([^'"]+)['"]|import\s*['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)""")


def ts_resolve(path: str, spec: str, files: set[str]) -> str | None:
    if not spec.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
    cands = [base, re.sub(r"\.(m|c)?jsx?$", ".ts", base), base + ".ts", base + "/index.ts", re.sub(r"\.js$", "", base) + "/index.ts"]
    return next((c for c in cands if c in files), None)


# ---------- Java ----------
def java_fqn(path: str, text: str) -> tuple[str, str]:
    m = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
    cls = posixpath.basename(path)[:-5]
    return (m.group(1) if m else ""), cls


def strip_test_name(stem: str) -> str:
    s = re.sub(r"^(test_)", "", stem)
    s = re.sub(r"(_test|Tests?|\.test|\.spec)$", "", s)
    return s.lower()


def main() -> None:
    for name in sys.argv[1:] or REPO_NAMES:
        corpus = load_corpus(name)
        import re as _re

        test_re = _re.compile(corpus["test_re"])
        exts = EXTS[name]
        files = sorted(f for f in git(REPOS / name, "ls-files").splitlines() if f.endswith(exts))
        fset = set(files)
        text = {f: read(name, f) for f in files}
        with Timer() as tb:
            rev: dict[str, set[str]] = {f: set() for f in files}  # file -> importers
            if name == "flask":
                mod2file: dict[str, str] = {}
                for f in files:
                    for m in py_module_names(f):
                        mod2file.setdefault(m, f)
                for f in files:
                    for m in py_imports(f, text[f]):
                        tgt = mod2file.get(m)
                        if tgt and tgt != f:
                            rev[tgt].add(f)
            elif name == "ky":
                for f in files:
                    for m in TS_IMPORT.finditer(text[f]):
                        spec = next(g for g in m.groups() if g)
                        tgt = ts_resolve(f, spec, fset)
                        if tgt and tgt != f:
                            rev[tgt].add(f)
            else:  # java
                fqn = {f: java_fqn(f, text[f]) for f in files}
                by_fqn = {f"{p}.{c}" if p else c: f for f, (p, c) in fqn.items()}
                by_pkg: dict[str, list[str]] = {}
                for f, (p, _c) in fqn.items():
                    by_pkg.setdefault(p, []).append(f)
                for f in files:
                    pkg = fqn[f][0]
                    for m in re.finditer(r"^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;", text[f], re.M):
                        imp = m.group(0)
                        target = m.group(1)
                        if imp.rstrip().endswith("*;"):
                            for g in by_pkg.get(target, []):
                                if g != f:
                                    rev[g].add(f)
                            continue
                        # strip trailing member for static imports / nested classes
                        parts = target.split(".")
                        while parts:
                            tgt = by_fqn.get(".".join(parts))
                            if tgt:
                                if tgt != f:
                                    rev[tgt].add(f)
                                break
                            parts.pop()
                    for g in by_pkg.get(pkg, []):  # same package: word match on class name
                        if g != f and re.search(r"\b" + re.escape(fqn[g][1]) + r"\b", text[f]):
                            rev[g].add(f)
        by_stem: dict[str, list[str]] = {}
        for f in files:
            if test_re.search(f):
                stem = posixpath.basename(f).rsplit(".", 1)[0]
                by_stem.setdefault(strip_test_name(stem), []).append(f)

        def common_prefix(a: str, b: str) -> int:
            n = 0
            for x, y in zip(a.split("/")[:-1], b.split("/")[:-1]):
                if x != y:
                    break
                n += 1
            return n

        queries = []
        for c in corpus["commits"]:
            seed = c["seed"]
            t0 = time.perf_counter()
            stem = strip_test_name(posixpath.basename(seed).rsplit(".", 1)[0])
            tests = sorted(by_stem.get(stem, []))
            imps = sorted(rev.get(seed, set()), key=lambda x: (-common_prefix(seed, x), x))
            ranked = dedupe(tests + imps, seed)
            queries.append(
                {
                    "sha": c["sha"],
                    "seed": seed,
                    "ranked": ranked,
                    "tests_for": tests,
                    "importers": imps,
                    "t_s": time.perf_counter() - t0,
                }
            )
        save_raw("baseline", name, {"tool": "baseline", "repo": name, "build_s": tb.s, "index_bytes": 0, "errors": [], "queries": queries})
        print(f"[baseline] {name}: scan {tb.s:.2f}s, files={len(files)}, nonempty={sum(1 for q in queries if q['ranked'])}/{len(queries)}", flush=True)


main()
