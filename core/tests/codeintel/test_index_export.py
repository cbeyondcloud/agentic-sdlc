"""The opt-in index export (M2-44, D39; spike S9): a deterministic, path-independent
sharded text export of the local index, and an import pinned to the tool and schema
version that, with an incremental update, equals a fresh build."""

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agentic.codeintel.export import (
    MANIFEST,
    ROOT_TOKEN,
    ExportError,
    ExportMismatch,
    check_tool,
    export_index,
    read_export,
)
from agentic.codeintel.index import LocalIndex, available

needs_crg = pytest.mark.skipif(not available(), reason="needs the codeintel extra")
GOLDEN = Path(__file__).parent / "golden" / "export-manifest.json"
#: Fixed identity and dates: the commit, and so the export, is the same on every machine.
COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "Dev",
    "GIT_AUTHOR_EMAIL": "dev@example.invalid",
    "GIT_COMMITTER_NAME": "Dev",
    "GIT_COMMITTER_EMAIL": "dev@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}
FILES = {
    "pkg/__init__.py": "",
    "pkg/models.py": "class Model:\n    def name(self):\n        return 'model'\n",
    "pkg/service.py": "from pkg.models import Model\n\n\ndef serve():\n    return Model().name()\n",
    "pkg/api.py": "from pkg.service import serve\n\n\ndef handle():\n    return serve()\n",
    "tests/__init__.py": "",
    "tests/test_models.py": (
        "from pkg.models import Model\n\n\ndef test_name():\n    assert Model().name() == 'model'\n"
    ),
}


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), "-c", "core.autocrlf=false", *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **COMMIT_ENV},
    )
    return done.stdout.strip()


def write(repo: Path, files: dict[str, str]) -> None:
    """LF bytes on every OS (write_text would write CRLF on Windows)."""
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


def make_repo(repo: Path, files: dict[str, str] = FILES) -> Path:
    write(repo, files)
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def built(repo: Path, data: Path, **kwargs) -> LocalIndex:
    index = LocalIndex(repo, data, home_dir=data / "home", **kwargs)
    index.ensure()
    return index


def folder_bytes(folder: Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()
    }


# ── export ───────────────────────────────────────────────────────────────────


@needs_crg
def test_export_is_byte_identical_across_rebuilds_and_roots(tmp_path):
    first = make_repo(tmp_path / "ci" / "repo")
    second = make_repo(tmp_path / "laptop" / "a checkout with spaces")
    a = export_index(built(first, tmp_path / "d1").db, first, tmp_path / "out1")
    again = export_index(built(first, tmp_path / "d2").db, first, tmp_path / "out2")
    other = export_index(built(second, tmp_path / "d3").db, second, tmp_path / "out3")

    assert a == again == other
    assert folder_bytes(tmp_path / "out1") == folder_bytes(tmp_path / "out2")
    assert folder_bytes(tmp_path / "out1") == folder_bytes(tmp_path / "out3")
    for name, data in folder_bytes(tmp_path / "out1").items():
        assert b"\r\n" not in data, name
        for root in (first, second):
            assert root.resolve().as_posix().encode() not in data, name
            assert str(root.resolve()).encode() not in data, name


@needs_crg
def test_export_matches_the_golden_manifest(tmp_path):
    # the same bytes on Windows and Linux: the manifest holds every shard's sha256
    repo = make_repo(tmp_path / "repo")
    export_index(built(repo, tmp_path / "data").db, repo, tmp_path / "out")
    produced = (tmp_path / "out" / MANIFEST).read_bytes()
    if os.environ.get("AGENTIC_WRITE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_bytes(produced)
    assert produced == GOLDEN.read_bytes()


@needs_crg
def test_the_manifest_names_tool_schema_commit_and_files(tmp_path):
    repo = make_repo(tmp_path / "repo")
    manifest = export_index(built(repo, tmp_path / "data").db, repo, tmp_path / "out")

    assert manifest["format"] == 1 and manifest["tool"] == "code-review-graph"
    assert manifest["tool_version"] == "2.3.9" and isinstance(manifest["schema_version"], int)
    assert manifest["commit"] == git(repo, "rev-parse", "HEAD")
    assert manifest["root_token"] == ROOT_TOKEN
    assert set(manifest["files"]) == {rel for rel in FILES}
    shard = manifest["files"]["pkg/models.py"]
    lines = (tmp_path / "out" / shard["shard"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == shard["nodes"] + shard["edges"]
    assert all(json.loads(line)["file_path"] == f"{ROOT_TOKEN}/pkg/models.py" for line in lines)
    assert not any(key in manifest["metadata"] for key in ("repo_root", "last_updated"))


# ── synthetic databases: rewriting and refusals ──────────────────────────────


def synthetic_db(path: Path, nodes: list[dict], edges: list[dict] = ()) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "create table nodes (id integer primary key, kind text, name text, "
        "qualified_name text, file_path text, extra text, updated_at real)"
    )
    con.execute(
        "create table edges (id integer primary key, kind text, source_qualified text, "
        "target_qualified text, file_path text, updated_at real)"
    )
    con.execute("create table metadata (key text primary key, value text)")
    con.execute("insert into metadata values ('schema_version', '13')")
    con.execute("insert into metadata values ('git_head_sha', 'abc')")
    for row in nodes:
        cols = ", ".join(row)
        con.execute(
            f"insert into nodes ({cols}) values ({', '.join('?' * len(row))})", [*row.values()]
        )
    for row in edges:
        cols = ", ".join(row)
        con.execute(
            f"insert into edges ({cols}) values ({', '.join('?' * len(row))})", [*row.values()]
        )
    con.commit()
    con.close()
    return path


def node(root: str, rel: str, **extra) -> dict:
    path = f"{root}/{rel}"
    return {"kind": "File", "name": path, "qualified_name": path, "file_path": path, **extra}


def test_root_is_replaced_only_at_a_path_boundary(tmp_path):
    tree = tmp_path / "w" / "repo"
    tree.mkdir(parents=True)
    root = tree.resolve().as_posix()
    db = synthetic_db(
        tmp_path / "g.db",
        [node(root, "a.py", extra=json.dumps({"seen": root}))],
        [
            {
                "kind": "CALLS",
                "source_qualified": f"{root}/a.py::f",
                "target_qualified": "g",
                "file_path": f"{root}/a.py",
            }
        ],
    )
    manifest = export_index(db, tree, tmp_path / "out", tool_version="0-test")
    lines = (tmp_path / "out" / manifest["files"]["a.py"]["shard"]).read_text("utf-8")
    assert f'"{ROOT_TOKEN}/a.py"' in lines and f"{ROOT_TOKEN}/a.py::f" in lines
    assert json.loads(json.loads(lines.splitlines()[0])["extra"]) == {"seen": ROOT_TOKEN}


def test_a_sibling_with_the_roots_prefix_is_never_rewritten_but_refused(tmp_path):
    tree = tmp_path / "w" / "repo"
    tree.mkdir(parents=True)
    root = tree.resolve().as_posix()
    db = synthetic_db(tmp_path / "g.db", [node(root, "a.py", extra=f"{root}2/b.py")])
    with pytest.raises(ExportError, match="absolute path"):
        export_index(db, tree, tmp_path / "out", tool_version="0-test")


def test_a_file_outside_the_tree_is_refused(tmp_path):
    tree = tmp_path / "w" / "repo"
    tree.mkdir(parents=True)
    elsewhere = (tmp_path / "elsewhere").resolve().as_posix()
    db = synthetic_db(tmp_path / "g.db", [node(elsewhere, "a.py")])
    with pytest.raises(ExportError, match="outside"):
        export_index(db, tree, tmp_path / "out", tool_version="0-test")


def test_shard_names_are_safe_on_every_file_system(tmp_path):
    # Linux allows a ':' and two names differing only in case; Windows/macOS do not
    tree = tmp_path / "repo"
    tree.mkdir()
    root = tree.resolve().as_posix()
    db = synthetic_db(
        tmp_path / "g.db", [node(root, "A.py"), node(root, "a.py"), node(root, "x:y.py")]
    )
    manifest = export_index(db, tree, tmp_path / "out", tool_version="0-test")
    shards = [entry["shard"] for entry in manifest["files"].values()]
    assert len({s.lower() for s in shards}) == 3
    for shard in shards:
        assert shard.startswith("shards/") and shard.endswith(".jsonl")
        assert all(c in "0123456789abcdef/." for c in shard.removeprefix("shards/")[:-6])


@pytest.mark.parametrize("planted", ["a-file.txt", ".git"])
def test_out_is_only_replaced_when_it_is_an_earlier_export(tmp_path, planted):
    tree = tmp_path / "repo"
    tree.mkdir()
    db = synthetic_db(tmp_path / "g.db", [node(tree.resolve().as_posix(), "a.py")])
    out = tmp_path / "out"
    export_index(db, tree, out, tool_version="0-test")
    export_index(db, tree, out, tool_version="0-test")  # an earlier export: replaced
    (out / planted).write_text("keep me", encoding="utf-8")
    with pytest.raises(ExportError, match="not an earlier export"):
        export_index(db, tree, out, tool_version="0-test")
    assert (out / planted).read_text(encoding="utf-8") == "keep me"


# ── import ───────────────────────────────────────────────────────────────────


def dump(index: LocalIndex) -> dict[str, list[str]]:
    """Every node and edge, derived columns included, with the root made relative."""
    root = index.tree.resolve().as_posix()
    rows: dict[str, list[str]] = {}
    with sqlite3.connect(index.db) as con:
        con.row_factory = sqlite3.Row
        for table in ("nodes", "edges"):
            found = []
            for row in con.execute(f"select * from {table}"):
                record = {
                    key: row[key].replace(root, ROOT_TOKEN)
                    if isinstance(row[key], str)
                    else row[key]
                    for key in row.keys()
                    if key not in ("id", "updated_at", "community_id")
                }
                found.append(json.dumps(record, sort_keys=True))
            rows[table] = sorted(found)
    return rows


def clone(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    git(source.parent, "clone", "-q", str(source), str(target))
    return target


def spied(index: LocalIndex) -> list[str]:
    ran: list[str] = []
    real = index._run

    def spy(command: str, *args: str) -> str:
        ran.append(command)
        return real(command, *args)

    index._run = spy
    return ran


@pytest.fixture
def exported(tmp_path) -> tuple[Path, Path]:
    """The CI checkout and its export."""
    ci = make_repo(tmp_path / "ci" / "repo")
    export_index(built(ci, tmp_path / "ci-data").db, ci, tmp_path / "export")
    return ci, tmp_path / "export"


@needs_crg
def test_a_seed_is_imported_instead_of_built_and_round_trips(exported, tmp_path):
    ci, export = exported
    laptop = clone(ci, tmp_path / "laptop" / "my checkout")
    index = LocalIndex(laptop, tmp_path / "d", home_dir=tmp_path / "d" / "home", seed=export)
    ran = spied(index)
    index.ensure()

    assert "build" not in ran and "update" not in ran  # the export is at HEAD already
    assert index.seed_note is None and index.fresh()
    export_index(index.db, laptop, tmp_path / "again")
    assert folder_bytes(tmp_path / "again") == folder_bytes(export)


@needs_crg
def test_import_then_update_equals_a_fresh_build(exported, tmp_path):
    ci, export = exported
    laptop = clone(ci, tmp_path / "laptop" / "repo")
    write(
        laptop,
        {
            "pkg/extra.py": "from pkg.service import serve\n\n\ndef more():\n    return serve()\n",
            "pkg/models.py": "class Model:\n    def name(self):\n        return 'x'\n\n"
            "    def other(self):\n        return 1\n",
        },
    )
    (laptop / "pkg" / "api.py").unlink()
    git(laptop, "add", "-A")
    git(laptop, "commit", "-qm", "ahead")
    write(laptop, {"pkg/wip.py": "from pkg.models import Model\n"})  # uncommitted, untracked

    imported = LocalIndex(laptop, tmp_path / "i", home_dir=tmp_path / "i" / "home", seed=export)
    ran = spied(imported)
    imported.ensure()
    fresh = built(laptop, tmp_path / "f")

    assert "build" not in ran and "update" in ran
    assert dump(imported) == dump(fresh)
    assert imported.indexed_files() == fresh.indexed_files()
    assert "pkg/wip.py" in imported.indexed_files() and "pkg/api.py" not in fresh.indexed_files()


@needs_crg
def test_names_with_unicode_line_separators_round_trip(tmp_path):
    # U+2028 and U+0085 stay raw in a shard (ensure_ascii=False); they end a line for
    # str.splitlines() but not for the export, whose lines end at "\n" only.
    rel = "pkg/odd and\u0085name.py"
    ci = make_repo(tmp_path / "ci" / "repo", {**FILES, rel: "def odd():\n    return 1\n"})
    export_index(built(ci, tmp_path / "ci-data").db, ci, tmp_path / "export")
    assert rel in read_export(tmp_path / "export")["files"]

    index = LocalIndex(ci, tmp_path / "d", home_dir=tmp_path / "d" / "home")
    index.load_export(tmp_path / "export")

    assert rel in index.indexed_files() and index.fresh()


def edit_manifest(export: Path, **changes) -> None:
    path = export / MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(changes)
    path.write_text(json.dumps(manifest), encoding="utf-8")


@needs_crg
def test_a_shard_that_is_not_json_is_refused_naming_the_file(exported, tmp_path):
    _, export = exported
    manifest = json.loads((export / MANIFEST).read_text(encoding="utf-8"))
    entry = manifest["files"]["pkg/models.py"]
    data = b'{"qualified_name": "unterminated\n'
    (export / entry["shard"]).write_bytes(data)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    (export / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    ci, _ = exported
    index = LocalIndex(ci, tmp_path / "d", home_dir=tmp_path / "d" / "home")

    with pytest.raises(ExportError) as refused:
        index.load_export(export)

    assert "pkg/models.py" in str(refused.value) and "line 1" in str(refused.value)
    assert not index.db.exists()


def test_a_different_tool_version_is_refused_naming_both(tmp_path):
    with pytest.raises(ExportMismatch) as refused:
        check_tool({"tool": "code-review-graph", "tool_version": "2.3.8"}, "2.3.9")
    assert "2.3.8" in str(refused.value) and "2.3.9" in str(refused.value)
    with pytest.raises(ExportError, match="not installed"):
        check_tool({"tool": "code-review-graph", "tool_version": "2.3.9"}, None)


@needs_crg
@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"tool_version": "2.3.8"}, "2.3.8"),
        ({"schema_version": 12}, "12"),
        ({"format": 2}, "format 2"),
        ({"tool": "another-indexer"}, "another-indexer"),
    ],
)
def test_a_mismatched_export_is_refused_and_writes_nothing(exported, tmp_path, changes, named):
    ci, export = exported
    edit_manifest(export, **changes)
    index = LocalIndex(ci, tmp_path / "d", home_dir=tmp_path / "d" / "home")
    with pytest.raises(ExportMismatch, match=named):
        index.load_export(export)
    assert not index.db.exists()
    assert [p.name for p in index.data_dir.iterdir() if p.name.startswith("graph")] == []


@needs_crg
def test_a_tampered_shard_is_refused(exported):
    _, export = exported
    manifest = json.loads((export / MANIFEST).read_text(encoding="utf-8"))
    shard = export / manifest["files"]["pkg/models.py"]["shard"]
    shard.write_bytes(shard.read_bytes().replace(b"Model", b"Modal"))
    with pytest.raises(ExportError, match="sha256"):
        read_export(export)


@needs_crg
@pytest.mark.parametrize("shard", ["../outside.jsonl", "/etc/passwd", "shards/../../x.jsonl"])
def test_unsafe_shard_names_are_refused(exported, shard):
    _, export = exported
    manifest = json.loads((export / MANIFEST).read_text(encoding="utf-8"))
    manifest["files"]["pkg/models.py"]["shard"] = shard
    (export / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExportError, match="shard"):
        read_export(export)


def test_a_folder_without_a_manifest_is_no_export(tmp_path):
    with pytest.raises(ExportError, match="not an index export"):
        read_export(tmp_path)


@needs_crg
def test_a_failed_import_keeps_the_old_index(exported, tmp_path):
    ci, export = exported
    index = built(ci, tmp_path / "d")
    before = dump(index)
    edit_manifest(export, schema_version=99)
    with pytest.raises(ExportMismatch):
        index.load_export(export)
    assert dump(index) == before and index.fresh()


@needs_crg
def test_a_seed_that_cannot_be_imported_falls_back_to_a_build(exported, tmp_path):
    ci, export = exported
    edit_manifest(export, tool_version="0.0.1")
    index = LocalIndex(ci, tmp_path / "d", home_dir=tmp_path / "d" / "home", seed=export)
    ran = spied(index)
    index.ensure()
    assert "build" in ran and index.fresh()
    assert "0.0.1" in index.seed_note
