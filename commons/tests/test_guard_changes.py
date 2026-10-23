"""`Guard.changes`: the post-step check of an agent CLI's writes (M2-60, review F4) gives
the same answer as `Guard.writes` on the files a step changed, at a cost that stays small
when a step leaves thousands of them (vendoring, a generated tree)."""

import os
from pathlib import Path

import pytest
from test_guards import DENY_PATHS, DENY_SHELL, RUN
from test_guards import repo as repo  # noqa: F401 - a fixture
from test_guards import tree as tree  # noqa: F401 - a fixture

from sdlc_commons.guards import Guard

TRICKY = (
    "src/app.py",
    "src/ok/readme.md",
    ".env",
    "pkg/.env",
    "certs/server.pem",
    "certs/server.PEM",
    "certs/server.pem.",  # how Windows stores `server.pem` (Zyroflo T499)
    "certs/server.pem ",
    "certs/server.pem:stream",
    "infra/prod/main.tf",
    "Infra/Prod/main.tf",
    "infra/staging/main.tf",
    ".github/workflows/ci.yml",
    ".GitHub/Workflows/ci.yml",
    "deep/er/.claude/settings.json",
    "AGENTS.md",
    "docs/AGENTS.md",
    "docs/agents.md",
    "metrics/run.jsonl",
    "a/metrics/run.jsonl",
    "with space/file.txt",
)


@pytest.fixture
def guard(repo: Path) -> Guard:
    return Guard(repo, RUN, deny_shell=DENY_SHELL, deny_paths=DENY_PATHS)


def _whys(blocks) -> list[tuple[str, str]]:
    return sorted((b.wall, b.why) for b in blocks)


@pytest.mark.parametrize("rel", TRICKY)
def test_changes_answers_as_writes_does(guard, tree, rel):
    paths = [tree / rel]
    assert _whys(guard.changes(paths)) == _whys(guard.writes(paths))


def test_a_file_reached_through_a_link_is_judged_in_full(guard, tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, tree / "link", target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi  # Windows without the symlink privilege: a junction instead

        _winapi.CreateJunction(str(outside), str(tree / "link"))
    paths = [tree / "link" / "file.txt", tree / "link"]
    found = guard.changes(paths)
    assert found and _whys(found) == _whys(guard.writes(paths))


def test_only_files_a_rule_could_match_are_judged_in_full(guard, tree, monkeypatch):
    judged: list[int] = []
    full = Guard.writes

    def counting(self, paths):
        paths = list(paths)
        judged.append(len(paths))
        return full(self, paths)

    monkeypatch.setattr(Guard, "writes", counting)
    many = [tree / "vendor" / f"pkg{n}" / "module.py" for n in range(500)]
    found = guard.changes([*many, tree / "certs" / "server.pem"])

    assert [b.wall for b in found] == ["denied-path"]
    assert sum(judged) <= 1  # the one suspect, never the 500 harmless files
