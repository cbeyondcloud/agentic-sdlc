"""`load_policy(root, version=...)` and `tree_digest` (SP15 Task 7): a policy fetched from the
control plane carries the version the control plane validated, and the extracted tree has a
digest that is the same on Windows, macOS and Linux."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, load_policy, tree_digest

COMMIT = "git:" + "a1" * 20


@pytest.fixture
def policy_dir(fixtures: Path, tmp_path: Path) -> Path:
    target = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", target)
    return target


def test_load_policy_accepts_explicit_version(policy_dir: Path):
    assert load_policy(policy_dir, version=COMMIT).version == COMMIT
    content = "sha256:" + "0" * 64
    assert load_policy(policy_dir, version=content).version == content


@pytest.mark.parametrize("bad", ["git:abc", "main", "sha256:XYZ", "git:" + "a1" * 20 + "\n", ""])
def test_load_policy_refuses_a_malformed_version(policy_dir: Path, bad: str):
    with pytest.raises(PolicyError, match="version"):
        load_policy(policy_dir, version=bad)


def test_load_policy_without_version_is_unchanged(policy_dir: Path):
    assert load_policy(policy_dir).version.startswith(("git:", "sha256:"))


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_policy_tree_digest_is_length_prefixed(tmp_path: Path):
    one = _tree(tmp_path / "one", {"a": b"bc"})
    two = _tree(tmp_path / "two", {"ab": b"c"})
    assert tree_digest(one) != tree_digest(two)
    three = _tree(tmp_path / "three", {"a": b"bc"})
    assert tree_digest(one) == tree_digest(three)
    assert tree_digest(one).startswith("sha256:")


def test_tree_digest_covers_names_bytes_and_nesting_but_not_git(tmp_path: Path):
    base = _tree(tmp_path / "base", {"policy/org.yaml": b"x: 1\n", "teams/p/agents/a.md": b"A"})
    same = _tree(tmp_path / "same", {"teams/p/agents/a.md": b"A", "policy/org.yaml": b"x: 1\n"})
    (same / ".git").mkdir()
    (same / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    assert tree_digest(base) == tree_digest(same)
    crlf = _tree(tmp_path / "crlf", {"policy/org.yaml": b"x: 1\r\n", "teams/p/agents/a.md": b"A"})
    assert tree_digest(base) != tree_digest(crlf)  # bytes, exactly: a local edit shows
    moved = _tree(tmp_path / "moved", {"policy/org.yaml": b"x: 1\n", "teams/p/a.md": b"A"})
    assert tree_digest(base) != tree_digest(moved)


def test_tree_digest_counts_an_empty_file(tmp_path: Path):
    base = _tree(tmp_path / "base", {"a": b"1"})
    extra = _tree(tmp_path / "extra", {"a": b"1", "b": b""})
    assert tree_digest(base) != tree_digest(extra)
