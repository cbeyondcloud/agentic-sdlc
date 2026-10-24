"""`agentic index export | import | publish` (M2-44, D39; spec §10.3): CI builds the index
once and publishes the export outside `main`; developers and runs start from it."""

import json
from pathlib import Path

import pytest
from cli_helpers import git

from agentic.codeintel.export import MANIFEST, read_export
from agentic.codeintel.index import available
from agentic_cli import codeintel
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
