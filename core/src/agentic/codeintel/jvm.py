"""Java and Kotlin by convention: no binary, no index (M3-27, spike S5 addendum).

code-review-graph's test edges into same-package Java tests mostly do not resolve, so on
google/gson raw `impact` found none of the changed tests (S5). Maven and Gradle projects
follow conventions that answer most of it from file names and the first lines of each
file:

  * tests: under a test source set (`src/test/java`, `src/integrationTest/kotlin`,
    `src/jvmTest/...`, `src/testFixtures/...`); under a source set (`src/main`,
    `src/commonMain`, ...) never; anywhere else (Ant's `src/com/...`, `src/jmh`), by a
    test-like name;
  * `tests_for(Foo)`, best first:
    1. tests named `FooTest`, `FooTests`, `FooIT`, `FooITCase`, `FooIntegrationTest`,
       `FooSpec` or `TestFoo`, in Foo's package;
    2. the same names in another package;
    3. tests in Foo's package whose name starts with `Foo` (`FooPathTest`);
    4. tests in Foo's package that name `Foo` (no import needed there);
    5. tests that import it (`import p.Foo`, `import static p.Foo.x`, `import p.*` with
       `Foo` used) or name it fully qualified;
  * `dependents(Foo)`: its tests, then the sources that import it, then the sources in
    its package that name it;
  * `impact(diff)`: the JVM files the diff names, then the dependents of the first
    `MAX_IMPACT_SEEDS` of them.

Only regular files inside the tree are read (links and junctions are never followed),
at most `MAX_FILES` of them and `MAX_READ_BYTES` of each, in a walk of at most
`MAX_ENTRIES` folder entries that skips hidden folders, and build output outside `src/`.
Answers are candidates, like every adapter's.
"""

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from agentic.codeintel.crg import diff_files  # one diff parser for every adapter
from agentic.codeintel.interface import Candidates
from agentic.codeintel.tools import tree_path

#: A repository with one of these at its root, or one folder down (a multi-service
#: repository), is a JVM (Maven or Gradle) repository.
JVM_BUILD_FILES = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
)
JVM_SUFFIXES = (".java", ".kt", ".groovy", ".scala")
#: Build output, skipped outside `src/` (inside it these are package names).
SKIPPED_DIRS = frozenset({"target", "build", "out", "bin", "node_modules"})
MAX_FILES = 20_000  # JVM files read per answer; more and the answer says it is partial
MAX_ENTRIES = 500_000  # folder entries looked at per answer
MAX_READ_BYTES = 64 * 1024  # of each file: the package, the imports and the first uses
MAX_IMPACT_SEEDS = 50  # changed files whose dependents `impact` adds
_TEST_SETS = frozenset({"test", "tests", "it", "integration-test", "testIntegration"})
_TEST_NAME = re.compile(r"^Test[A-Z_].*|.*(Test|Tests|IT|ITCase|Spec)$")
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)", re.MULTILINE)
_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?(\w+(?:\.\w+)*(?:\.\*)?)", re.MULTILINE)


def is_jvm_repo(root: Path) -> bool:
    """Whether `root` has a Maven or Gradle build file, at its root or one folder down
    (routing by language, M3-27)."""
    if any((root / name).is_file() for name in JVM_BUILD_FILES):
        return True
    try:
        folders = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return False
    return any(
        (Path(folder.path) / name).is_file()
        for folder in folders
        if not folder.name.startswith(".") and folder.name not in SKIPPED_DIRS
        for name in JVM_BUILD_FILES
    )


def _is_jvm(rel: str) -> bool:
    return rel.endswith(JVM_SUFFIXES)


def _is_test(rel: str) -> bool:
    parts = rel.split("/")
    for i, part in enumerate(parts[:-2]):
        if part != "src":
            continue
        source_set = parts[i + 1]
        if (
            source_set in _TEST_SETS
            or source_set.startswith("test")
            or source_set.endswith(("Test", "Tests"))
        ):
            return True
        if source_set == "main" or source_set.endswith("Main"):
            return False
        break  # Ant's src/com/..., src/jmh, src/generated: by name
    return _TEST_NAME.fullmatch(_stem(rel)) is not None


def _stem(rel: str) -> str:
    return rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]


@dataclass(frozen=True)
class _File:
    rel: str
    text: str

    @cached_property
    def package(self) -> str:
        found = _PACKAGE.search(self.text)
        return found.group(1) if found else ""

    @cached_property
    def imports(self) -> tuple[str, ...]:
        return tuple(_IMPORT.findall(self.text))

    @property
    def stem(self) -> str:
        return _stem(self.rel)

    @property
    def test(self) -> bool:
        return _is_test(self.rel)


class JvmConventions:
    name = "jvm-conventions"
    supports = frozenset({"dependents", "impact", "tests_for"})

    def __init__(self, tree: Path) -> None:
        self.tree = Path(tree)

    def tests_for(self, path: str) -> Candidates:
        files, partial = self._scan()
        seed = self._seed(path, files)
        if isinstance(seed, Candidates):
            return seed
        return self._answer(self._tests(seed, files), partial)

    def dependents(self, path: str) -> Candidates:
        files, partial = self._scan()
        seed = self._seed(path, files)
        if isinstance(seed, Candidates):
            return seed
        return self._answer(self._tests(seed, files) + self._users(seed, files), partial)

    def impact(self, diff: str) -> Candidates:
        named = (tree_path(self.tree, raw) for raw in diff_files(diff))
        changed = [rel for rel in dict.fromkeys(named) if rel and _is_jvm(rel)]
        files, partial = self._scan()
        found = [rel for rel in changed if rel in files or self._read(rel)]
        if not found:
            return Candidates((), source=self.name, note="the diff names no JVM source file")
        merged = list(found)
        for rel in found[:MAX_IMPACT_SEEDS]:
            seed = files.get(rel) or self._read(rel)
            if seed is not None and not seed.test:
                merged += self._tests(seed, files) + self._users(seed, files)
        answer = self._answer(merged, partial)
        if len(found) > MAX_IMPACT_SEEDS:
            more = f"dependents of the first {MAX_IMPACT_SEEDS} changed files only"
            answer = Candidates(
                answer.files, answer.source, "\n".join(filter(None, [answer.note, more]))
            )
        return answer

    # ── the conventions ──────────────────────────────────────────────────────

    def _tests(self, seed: _File, files: dict[str, _File]) -> list[str]:
        if seed.test:
            return []
        name = seed.stem
        named = {f"{name}{end}" for end in ("Test", "Tests", "IT", "ITCase", "Spec")}
        named |= {f"{name}IntegrationTest", f"Test{name}"}
        mention = re.compile(rf"\b{re.escape(name)}\b")
        tiers: list[list[str]] = [[] for _ in range(5)]
        for rel, other in files.items():
            if rel == seed.rel or not other.test:
                continue
            here = other.package == seed.package
            if other.stem in named:
                tiers[0 if here else 1].append(rel)
            elif here and other.stem.startswith(name):
                tiers[2].append(rel)
            elif here and mention.search(other.text):
                tiers[3].append(rel)
            elif self._imports(other, seed, mention):
                tiers[4].append(rel)
        return [rel for tier in tiers for rel in sorted(tier)]

    def _users(self, seed: _File, files: dict[str, _File]) -> list[str]:
        mention = re.compile(rf"\b{re.escape(seed.stem)}\b")
        importers, neighbours = [], []
        for rel, other in files.items():
            if rel == seed.rel or other.test:
                continue
            if self._imports(other, seed, mention):
                importers.append(rel)
            elif other.package == seed.package and mention.search(other.text):
                neighbours.append(rel)
        return sorted(importers) + sorted(neighbours)

    @staticmethod
    def _imports(other: _File, seed: _File, mention: re.Pattern[str]) -> bool:
        if not seed.package or other.package == seed.package:
            return False
        qualified = f"{seed.package}.{seed.stem}"
        for name in other.imports:
            if name == qualified or name.startswith(qualified + "."):
                return True
            if name == f"{seed.package}.*" and mention.search(other.text):
                return True
        return re.search(rf"\b{re.escape(qualified)}\b", other.text) is not None

    # ── reading the tree ─────────────────────────────────────────────────────

    def _seed(self, path: str, files: dict[str, _File]) -> "_File | Candidates":
        rel = tree_path(self.tree, path)
        if rel is None or not _is_jvm(rel):
            return Candidates((), source=self.name, note=f"{path} is not a JVM source file")
        seed = files.get(rel) or self._read(rel)  # read on its own past the file cap
        if seed is None:
            return Candidates((), source=self.name, note=f"{rel} is not a file in the tree")
        return seed

    def _answer(self, found: list[str], partial: bool) -> Candidates:
        note = f"only the first {MAX_FILES} JVM files were read" if partial else ""
        return Candidates(tuple(dict.fromkeys(found)), source=self.name, note=note)

    def _read(self, rel: str) -> _File | None:
        """One regular file of the tree, never through a link (None otherwise)."""
        full = self.tree.resolve() / rel
        try:
            if any(p.is_symlink() or p.is_junction() for p in (full, *full.parents)):
                return None
            if not full.is_file():
                return None
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(full, flags)
            with os.fdopen(descriptor, "rb") as handle:
                text = handle.read(MAX_READ_BYTES).decode("utf-8", errors="replace")
        except OSError:
            return None
        return _File(rel, text)

    def _scan(self) -> tuple[dict[str, _File], bool]:
        """Every JVM file in the tree, path to text (the first MAX_READ_BYTES), and
        whether the answer is partial (more than MAX_FILES, or MAX_ENTRIES looked at)."""
        files: dict[str, _File] = {}
        walked = self._walk(self.tree.resolve())
        for rel, full in walked:
            if rel is None or len(files) >= MAX_FILES:
                return files, True
            try:
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                with os.fdopen(os.open(full, flags), "rb") as handle:
                    text = handle.read(MAX_READ_BYTES).decode("utf-8", errors="replace")
            except OSError:
                continue
            files[rel] = _File(rel, text)
        return files, False

    @staticmethod
    def _walk(root: Path) -> Iterator[tuple[str | None, Path]]:
        """(tree-relative path, full path) of each JVM file, depth first in name order,
        without recursion; (None, root) once MAX_ENTRIES entries have been looked at."""
        stack: list[tuple[Path, str, bool]] = [(root, "", False)]  # folder, prefix, in src/
        seen = 0
        while stack:
            folder, prefix, in_src = stack.pop()
            try:
                entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
            except OSError:
                continue
            seen += len(entries)
            if seen > MAX_ENTRIES:
                yield None, root
                return
            folders = []
            for entry in entries:
                if entry.is_symlink() or entry.is_junction():
                    continue  # never leave the tree through a link
                rel = f"{prefix}{entry.name}"
                if entry.is_dir(follow_symlinks=False):
                    hidden = entry.name.startswith(".")
                    output = entry.name in SKIPPED_DIRS and not in_src
                    if not hidden and not output:
                        folders.append((Path(entry.path), rel + "/", in_src or entry.name == "src"))
                elif entry.is_file(follow_symlinks=False) and _is_jvm(entry.name):
                    yield rel, Path(entry.path)
            stack.extend(reversed(folders))
