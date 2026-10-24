"""`agentic index export | import | publish` (M2-44, D39; spec §10.3): CI builds the index
once and publishes the export outside `main`; developers and runs start from it."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from cli_helpers import git

from agentic.codeintel.export import MANIFEST, read_export
from agentic.codeintel.index import available
from agentic_cli import codeintel, index_publish
from agentic_cli.commands import index as index_command
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

needs_crg = pytest.mark.skipif(not available(), reason="needs the codeintel extra")
SOURCES = {
    "pkg/__init__.py": "",
    "pkg/models.py": "class Model:\n    def name(self):\n        return 'model'\n",
    "pkg/service.py": "from pkg.models import Model\n\n\ndef serve():\n    return Model().name()\n",
}


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", "Index Bot")
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "index-bot@example.invalid")


def commit(repo: Path, files: dict[str, str], message: str = "change") -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def ci(repo: Path) -> Path:
    """The repository as CI checks it out, with some Python in it."""
    commit(repo, SOURCES, "sources")
    return repo


def run(capsys, *args: str) -> tuple[int, dict]:
    code = main(["--json", "index", *args])
    out = capsys.readouterr()
    text = out.out.strip() or out.err.strip()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, {"raw": text}


def status(repo: Path) -> str:
    return git(repo, "status", "--porcelain", "--untracked-files=all")


# ── export ───────────────────────────────────────────────────────────────────


@needs_crg
def test_export_writes_a_verified_export_of_head_and_leaves_the_tree(ci, tmp_path, capsys):
    out = tmp_path / "export"
    code, data = run(capsys, "export", "--out", str(out), "--repo", str(ci))

    assert code == ExitCode.OK, data
    assert data["commit"] == git(ci, "rev-parse", "HEAD")
    assert data["files"] == len(read_export(out)["files"]) >= 3
    assert data["tool_version"] == "2.3.9" and data["out"] == str(out)
    assert status(ci) == "" and not (ci / ".agentic").exists()


@needs_crg
def test_export_refuses_a_tree_with_uncommitted_changes(ci, tmp_path, capsys):
    (ci / "pkg" / "models.py").write_text("changed = 1\n", encoding="utf-8")
    code, data = run(capsys, "export", "--out", str(tmp_path / "export"), "--repo", str(ci))
    assert code == ExitCode.REFUSED and "uncommitted" in json.dumps(data)
    assert not (tmp_path / "export").exists()


def test_export_never_replaces_a_folder_that_is_not_an_export(ci, tmp_path, capsys):
    out = tmp_path / "precious"
    out.mkdir()
    (out / "notes.txt").write_text("keep", encoding="utf-8")
    code, data = run(capsys, "export", "--out", str(out), "--repo", str(ci))
    assert code == ExitCode.REFUSED and "not an earlier export" in json.dumps(data)
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_export_needs_the_codeintel_extra(ci, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(index_command, "available", lambda: False)
    code, data = run(capsys, "export", "--out", str(tmp_path / "export"), "--repo", str(ci))
    assert code == ExitCode.ERROR and "agentic-core[codeintel]" in json.dumps(data)


# ── import ───────────────────────────────────────────────────────────────────


@pytest.fixture
def exported(ci, tmp_path, capsys) -> Path:
    out = tmp_path / "export"
    code, data = run(capsys, "export", "--out", str(out), "--repo", str(ci))
    assert code == ExitCode.OK, data
    return out


def laptop_clone(ci: Path, tmp_path: Path) -> Path:
    laptop = tmp_path / "laptop" / "app"
    laptop.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", str(ci), str(laptop))
    return laptop


@needs_crg
def test_import_seeds_the_runs_and_catches_the_local_index_up(ci, exported, tmp_path, capsys):
    laptop = laptop_clone(ci, tmp_path)
    head = commit(laptop, {"pkg/api.py": "from pkg.service import serve\n"}, "ahead")

    code, data = run(capsys, "import", str(exported), "--repo", str(laptop))

    assert code == ExitCode.OK, data
    assert data["commit"] == git(ci, "rev-parse", "HEAD") and data["recorded_commit"] == head
    seed = laptop / ".agentic" / "local" / "index-export"
    assert data["seed"] == str(seed) and read_export(seed)["commit"] == data["commit"]
    assert (laptop / ".agentic" / "local" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert status(laptop) == ""


@needs_crg
def test_import_refuses_another_tool_version_and_keeps_nothing(ci, exported, tmp_path, capsys):
    manifest = json.loads((exported / MANIFEST).read_text(encoding="utf-8"))
    manifest["tool_version"] = "2.3.1"
    (exported / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    laptop = laptop_clone(ci, tmp_path)

    code, data = run(capsys, "import", str(exported), "--repo", str(laptop))

    assert code == ExitCode.REFUSED
    assert "2.3.1" in json.dumps(data) and "2.3.9" in json.dumps(data)
    assert not (laptop / ".agentic" / "local" / "index-export").exists()


@needs_crg
def test_import_refuses_a_damaged_export_as_invalid(ci, exported, tmp_path, capsys):
    shard = next((exported / "shards").rglob("*.jsonl"))
    shard.write_bytes(shard.read_bytes() + b"{}\n")
    code, data = run(capsys, "import", str(exported), "--repo", str(laptop_clone(ci, tmp_path)))
    assert code == ExitCode.INVALID and "sha256" in json.dumps(data)


def test_import_takes_a_folder_or_a_ref_not_both(ci, tmp_path, capsys):
    code, _ = run(capsys, "import", str(tmp_path), "--ref", "origin/agentic-index")
    assert code == ExitCode.USAGE
    code, _ = run(capsys, "import", "--repo", str(ci))
    assert code == ExitCode.USAGE


def test_runs_start_from_the_imported_export(tmp_path):
    settings = codeintel.codeintel_settings({})
    make = codeintel.codeintel_for(tmp_path, settings, crg_available=True)
    assert make(tmp_path / "tree", tmp_path / "data").index.seed is None

    seed = tmp_path / ".agentic" / "local" / "index-export"
    seed.mkdir(parents=True)
    (seed / MANIFEST).write_text("{}", encoding="utf-8")
    make = codeintel.codeintel_for(tmp_path, settings, crg_available=True)
    assert make(tmp_path / "tree", tmp_path / "data").index.seed == seed


# ── publish: outside main only ───────────────────────────────────────────────


@pytest.fixture
def remote(ci, tmp_path) -> Path:
    """A bare remote whose default branch is `trunk`, holding the CI checkout's history
    on `main` and `trunk`; the checkout's `origin`."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(bare))
    git(ci, "remote", "set-url", "origin", str(bare))
    git(ci, "push", "-q", "origin", "main", "main:trunk")
    return bare


def plumbing(where: Path, *args: str, stdin: str | None = None, env: dict | None = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **(env or {})},
    )
    return done.stdout.strip()


def refs(where: Path) -> str:
    return git(where, "for-each-ref", "--format=%(refname) %(objectname)")


def branches(where: Path) -> str:
    """The local branches and tags (a push updates only remote-tracking refs)."""
    return git(
        where, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags"
    )


@needs_crg
def test_publish_moves_only_the_index_ref(ci, exported, remote, capsys):
    before_remote, before_local = refs(remote), branches(ci)
    head = git(ci, "rev-parse", "HEAD")

    code, data = run(capsys, "publish", str(exported), "--repo", str(ci))

    assert code == ExitCode.OK, data
    assert data["ref"] == "refs/heads/agentic-index" and data["pushed"] is True
    after = refs(remote).splitlines()
    assert sorted(set(after) - set(before_remote.splitlines())) == [
        f"refs/heads/agentic-index {data['commit']}"
    ]
    assert set(before_remote.splitlines()) <= set(after)  # main and trunk unchanged
    assert branches(ci) == before_local and git(ci, "rev-parse", "HEAD") == head
    assert status(ci) == ""
    published = git(remote, "ls-tree", "-r", "--name-only", "agentic-index").splitlines()
    assert MANIFEST in published and ".gitattributes" in published
    assert git(remote, "show", "agentic-index:.gitattributes") == "* -text"
    assert git(remote, "rev-list", "--count", "agentic-index") == "1"  # no history kept


@needs_crg
def test_publish_dry_run_writes_nothing(ci, exported, remote, capsys):
    before_remote, before_local = refs(remote), branches(ci)
    code, data = run(capsys, "publish", str(exported), "--dry-run", "--repo", str(ci))
    assert code == ExitCode.OK, data
    assert data["dry_run"] is True and data["pushed"] is False and len(data["commit"]) == 40
    assert refs(remote) == before_remote and branches(ci) == before_local and status(ci) == ""


@needs_crg
@pytest.mark.parametrize(
    "ref",
    ["main", "refs/heads/main", "master", "trunk", "feature", "refs/tags/v1", "refs/notes/x"],
)
def test_publish_never_targets_main_the_default_or_the_current_branch(
    ci, exported, remote, capsys, ref
):
    git(ci, "switch", "-q", "-c", "feature")
    before = refs(remote)
    code, data = run(capsys, "publish", str(exported), "--ref", ref, "--repo", str(ci))
    assert code == ExitCode.REFUSED, data
    assert refs(remote) == before


@needs_crg
@pytest.mark.parametrize("ref", ["release/1.0", "refs/heads/develop", "refs/agentic/kept"])
def test_publish_never_overwrites_history_that_is_not_an_index_export(
    ci, exported, remote, capsys, ref
):
    git(ci, "push", "-q", "origin", f"main:{index_publish.full_ref(ref)}")
    before = refs(remote)

    for dry_run in (["--dry-run"], []):
        code, data = run(
            capsys, "publish", str(exported), "--ref", ref, *dry_run, "--repo", str(ci)
        )
        assert code == ExitCode.REFUSED, data
        assert "not an index export" in json.dumps(data)
    assert refs(remote) == before


def export_again(ci: Path, tmp_path: Path, capsys, name: str, files: dict[str, str]) -> Path:
    commit(ci, files, name)
    out = tmp_path / name
    code, data = run(capsys, "export", "--out", str(out), "--repo", str(ci))
    assert code == ExitCode.OK, data
    return out


@needs_crg
def test_publish_commits_only_the_changed_shards_on_the_last_export(
    ci, exported, remote, tmp_path, capsys, monkeypatch
):
    code, first = run(capsys, "publish", str(exported), "--repo", str(ci))
    assert code == ExitCode.OK and first["pushed"] is True, first

    # A night with nothing new pushes nothing.
    before = refs(remote)
    code, same = run(capsys, "publish", str(exported), "--repo", str(ci))
    assert code == ExitCode.OK, same
    assert same["unchanged"] is True and same["pushed"] is False
    assert same["commit"] == first["commit"] and refs(remote) == before

    # A changed file: one child commit of the last export, holding only what changed.
    models = SOURCES["pkg/models.py"] + "\n\ndef helper():\n    return 1\n"
    second = export_again(ci, tmp_path, capsys, "export-2", {"pkg/models.py": models})
    code, data = run(capsys, "publish", str(second), "--repo", str(ci))
    assert code == ExitCode.OK and data["pushed"] is True and data["unchanged"] is False, data
    assert git(remote, "rev-parse", "agentic-index^") == first["commit"]
    assert git(remote, "rev-list", "--count", "agentic-index") == "2"
    old, new = (read_export(f)["files"] for f in (exported, second))
    changed = {MANIFEST} | {e["shard"] for rel, e in new.items() if old.get(rel) != e}
    diff = git(remote, "diff-tree", "--name-only", "-r", first["commit"], data["commit"])
    assert set(diff.splitlines()) == changed and len(changed) < len(new) + 1

    # After HISTORY nights the ref starts again from one commit, so fetches stay small.
    monkeypatch.setattr(index_publish, "HISTORY", 2)
    code, data = run(capsys, "publish", str(exported), "--repo", str(ci))
    assert code == ExitCode.OK and data["pushed"] is True, data
    assert git(remote, "rev-list", "--count", "agentic-index") == "1"
    assert git(remote, "rev-parse", "agentic-index^{tree}") == git(
        remote, "rev-parse", f"{first['commit']}^{{tree}}"
    )


@needs_crg
def test_publish_never_overwrites_a_ref_that_moved_after_it_looked(
    ci, exported, remote, capsys, monkeypatch
):
    code, first = run(capsys, "publish", str(exported), "--repo", str(ci))
    assert code == ExitCode.OK, first
    real = index_publish._tree

    def meanwhile(repo, files):
        # Another publisher moves the ref between this one's look and its push.
        other = git(ci, "commit-tree", f"{first['commit']}^{{tree}}", "-m", "other")
        git(ci, "push", "-q", "origin", f"+{other}:refs/heads/agentic-index")
        return real(repo, {**files, "changed": b"1\n"})

    monkeypatch.setattr(index_publish, "_tree", meanwhile)
    code, data = run(capsys, "publish", str(exported), "--repo", str(ci))

    assert code == ExitCode.ERROR, data
    assert git(remote, "log", "-1", "--format=%s", "agentic-index") == "other"


@needs_crg
def test_import_from_a_fetched_ref_equals_the_folder(ci, exported, remote, tmp_path, capsys):
    assert run(capsys, "publish", str(exported), "--repo", str(ci))[0] == ExitCode.OK
    laptop = laptop_clone(remote, tmp_path)
    git(laptop, "fetch", "-q", "origin", "agentic-index:refs/remotes/origin/agentic-index")

    code, data = run(capsys, "import", "--ref", "origin/agentic-index", "--repo", str(laptop))

    assert code == ExitCode.OK, data
    seed = Path(data["seed"])
    assert (seed / MANIFEST).read_bytes() == (exported / MANIFEST).read_bytes()
    assert data["recorded_commit"] == git(laptop, "rev-parse", "HEAD")


def test_import_from_a_ref_refuses_a_link(ci, tmp_path, capsys):
    blob = plumbing(ci, "hash-object", "-w", "--stdin", stdin="../../outside")
    scratch = {"GIT_INDEX_FILE": str(tmp_path / "scratch-index")}
    plumbing(
        ci, "update-index", "--add", "--cacheinfo", f"120000,{blob},manifest.json", env=scratch
    )
    tree = plumbing(ci, "write-tree", env=scratch)
    commit_id = git(ci, "commit-tree", tree, "-m", "planted")
    git(ci, "update-ref", "refs/remotes/origin/agentic-index", commit_id)

    code, data = run(capsys, "import", "--ref", "origin/agentic-index", "--repo", str(ci))
    assert code == ExitCode.INVALID and "manifest.json" in json.dumps(data)
    assert not (ci / ".agentic").exists()
