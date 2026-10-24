"""The opt-in index export (M2-44, D39; spike S9 `crg_export.py`).

A repo whose full local build is too slow has CI build the index once and publish this
export outside `main`; developers and runs import it and catch up with an incremental
`update`. The export is a function of the committed files only, so two builds of one
commit - in different checkout folders, on Windows or Linux - give the same bytes:

  * nodes and edges only (the parsed ground truth); the volatile columns (`id`,
    `updated_at`) and the derived ones (`community_id`, `signature`,
    `target_resolution`, rebuilt by the tool's post-processing on import) are dropped;
  * the tree's absolute root becomes `${ROOT}`, only where a path continues after it
    (`/`, `::`, a closing quote, the end); any other trace of the root is refused;
  * one JSONL shard per source file, its lines sorted, named by the sha256 of the
    repo-relative path (`shards/ab/<30 hex>.jsonl`): no character Windows refuses, no
    two names differing only in case, no deep path past Windows' path limit;
  * `manifest.json` (written last) with the format, tool, tool and schema version, the
    commit, the kept metadata and each file's shard, sha256 and counts. LF, UTF-8,
    sorted keys, no timestamps.

The import is pinned to the tool and schema version the manifest records (`check_tool`,
and `_crg_import` for the schema): another version is refused, never guessed at.
"""

import hashlib
import importlib.metadata
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from agentic.backends.processes import run_contained
from agentic.codeintel.interface import CodeIntelError

ROOT_TOKEN = "${ROOT}"
FORMAT = 1
TOOL = "code-review-graph"
MANIFEST = "manifest.json"
SHARDS = "shards"
DROPPED_NODE = frozenset({"id", "updated_at", "community_id", "signature"})
DROPPED_EDGE = frozenset({"id", "updated_at", "target_resolution"})
#: Metadata that names this machine, this moment or this build, not the code.
DROPPED_META = frozenset(
    {
        "schema_version",  # the manifest's own field
        "git_head_sha",  # the manifest's `commit`
        "git_branch",
        "repo_root",
        "last_updated",
        "last_build_type",
        "last_postprocessed_at",
        "postprocess_level",
        "build_state",
        "fts_state_synced",
    }
)
#: What an export folder may hold; anything else means it is not one (never deleted).
EXPORT_ENTRIES = frozenset({MANIFEST, SHARDS, ".gitattributes"})


class ExportError(CodeIntelError):
    """The export cannot be written or read; the message says why."""


class ExportMismatch(ExportError):
    """The export was made by another tool, tool version, schema or format."""


def installed_tool_version() -> str | None:
    """The installed code-review-graph version, without importing it."""
    try:
        return importlib.metadata.version(TOOL)
    except importlib.metadata.PackageNotFoundError:
        return None


def canonical_root(tree: Path) -> str:
    """The tree's root as code-review-graph records it: resolved, forward slashes."""
    return Path(tree).expanduser().resolve().as_posix()


def shard_name(rel: str) -> str:
    digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()
    return f"{SHARDS}/{digest[:2]}/{digest[2:32]}.jsonl"


def export_index(db: Path, tree: Path, out: Path, *, tool_version: str | None = None) -> dict:
    """Write the export of the index `db` (built from `tree`) to `out`; returns the
    manifest. `out` must be missing, empty or an earlier export (it is replaced)."""
    version = tool_version or installed_tool_version()
    if version is None:
        raise ExportError(f"{TOOL} is not installed (agentic-core[codeintel])")
    root = canonical_root(tree)
    native = str(Path(root))
    boundary = re.compile(re.escape(root) + r'(?=/|::|"|$)')
    traces = {root, native, json.dumps(native)[1:-1]}

    def rewrite(value: object) -> object:
        if not isinstance(value, str):
            return value
        value = boundary.sub(lambda _: ROOT_TOKEN, value)
        if any(trace in value for trace in traces):
            raise ExportError(
                f"the index holds an absolute path of this checkout that cannot be made "
                f"relative ({value[:120]!r}); the export would not be path-independent"
            )
        return value

    shards: dict[str, list[tuple[int, str]]] = {}
    try:
        with closing(sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            meta = dict(con.execute("select key, value from metadata").fetchall())
            for kind, table, dropped in ((0, "nodes", DROPPED_NODE), (1, "edges", DROPPED_EDGE)):
                for row in con.execute(f"select * from {table}"):
                    record = {
                        key: rewrite(row[key])
                        for key in row.keys()
                        if key not in dropped and row[key] is not None
                    }
                    rel = _relative(record.get("file_path"), row["file_path"])
                    shards.setdefault(rel, []).append((kind, _line(record)))
    except sqlite3.Error as error:
        raise ExportError(f"the code index cannot be read: {error}") from error
    if not meta.get("git_head_sha"):
        raise ExportError("the code index records no commit; build it from a git checkout")

    _clear(Path(out))
    files: dict[str, dict] = {}
    for rel in sorted(shards):
        lines = sorted(shards[rel])  # nodes, then edges, each by their whole line
        data = "".join(line + "\n" for _, line in lines).encode("utf-8")
        name = shard_name(rel)
        target = Path(out) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        nodes = sum(1 for kind, _ in lines if kind == 0)
        files[rel] = {
            "shard": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "nodes": nodes,
            "edges": len(lines) - nodes,
        }
    manifest = {
        "format": FORMAT,
        "tool": TOOL,
        "tool_version": version,
        "schema_version": int(meta.get("schema_version") or 0),
        "commit": meta["git_head_sha"],
        "root_token": ROOT_TOKEN,
        "metadata": {
            key: rewrite(value) for key, value in sorted(meta.items()) if key not in DROPPED_META
        },
        "files": files,
        "nodes": sum(entry["nodes"] for entry in files.values()),
        "edges": sum(entry["edges"] for entry in files.values()),
    }
    text = json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    (Path(out) / MANIFEST).write_bytes(text.encode("utf-8"))  # last: a partial one has none
    return manifest


def read_export(folder: Path) -> dict:
    """The manifest of the export in `folder`, checked: this format and tool, a commit,
    every shard named as the export names it (never a path of its own) and holding the
    bytes the manifest's sha256 says. Raises ExportError (ExportMismatch for another
    format or tool)."""
    folder = Path(folder)
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ExportError(f"{folder} is not an index export (no readable {MANIFEST})") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise ExportError(f"{folder / MANIFEST} is not an index export manifest")
    if manifest.get("format") != FORMAT:
        raise ExportMismatch(
            f"the index export is format {manifest.get('format')}; this agentic reads "
            f"format {FORMAT} - update agentic, or re-run the export with this version"
        )
    if manifest.get("tool") != TOOL:
        raise ExportMismatch(f"the index export was made by {manifest.get('tool')!r}, not {TOOL}")
    if not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", str(manifest.get("commit", ""))):
        raise ExportError(f"{folder / MANIFEST} names no commit")
    for rel, entry in manifest["files"].items():
        shard = entry.get("shard") if isinstance(entry, dict) else None
        if not isinstance(rel, str) or shard != shard_name(rel):
            raise ExportError(f"{folder / MANIFEST}: file {rel!r} names an unexpected shard")
        try:
            data = (folder / shard).read_bytes()
        except OSError as error:
            raise ExportError(f"the index export is missing the shard of {rel}") from error
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise ExportError(f"the shard of {rel} does not match its sha256: damaged or edited")
    return manifest


def check_tool(manifest: dict, installed: str | None) -> None:
    """Refuse an export made by another code-review-graph version than `installed`."""
    if installed is None:
        raise ExportError(f"{TOOL} is not installed (agentic-core[codeintel])")
    made = manifest.get("tool_version")
    if made != installed:
        raise ExportMismatch(
            f"the index export was made with {TOOL} {made} and this machine has {installed}; "
            f"an import is pinned to the version that made it. Re-run the export with "
            f"{installed}, or install {TOOL}=={made}"
        )


def import_export(
    folder: Path, tree: Path, target: Path, env: dict[str, str], *, timeout: float = 1800
) -> dict:
    """Write the index the export in `folder` holds, for `tree`, to the new file `target`
    (code-review-graph runs in its own process, `_crg_import`); returns the manifest.
    Raises ExportError, ExportMismatch for another tool, schema or format version."""
    manifest = read_export(folder)
    check_tool(manifest, installed_tool_version())
    request = {"folder": str(Path(folder).resolve()), "tree": str(tree), "target": str(target)}
    try:
        done = run_contained(
            [sys.executable, "-m", "agentic.codeintel._crg_import"],
            input=json.dumps(request),
            env=env,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExportError(f"the index import could not run: {error}") from error
    answer = _answer(done.stdout)
    if done.returncode != 0 or "error" in answer:
        Path(target).unlink(missing_ok=True)
        lines = done.stderr.strip().splitlines()
        why = answer.get("error") or (lines[-1] if lines else f"exit {done.returncode}")
        raise (ExportMismatch if answer.get("mismatch") else ExportError)(why)
    return manifest


def _answer(stdout: str) -> dict:
    """The import's JSON answer: the last line that is a JSON object."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict):
            return answer
    return {}


def _relative(rewritten: object, raw: str) -> str:
    prefix = ROOT_TOKEN + "/"
    if not isinstance(rewritten, str) or not rewritten.startswith(prefix):
        raise ExportError(f"the index holds a file outside the tree: {raw!r}")
    return rewritten[len(prefix) :]


def _line(record: dict) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _clear(out: Path) -> None:
    """Make `out` an empty folder: it may be missing, empty or an earlier export."""
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise ExportError(f"{out} is not a folder; give a new or empty folder for the export")
    if out.is_dir():
        entries = {entry.name for entry in out.iterdir()}
        if entries and (MANIFEST not in entries or not entries <= EXPORT_ENTRIES):
            raise ExportError(
                f"{out} is not an earlier export and not empty; give a new or empty folder"
            )
        for name in entries:
            path = out / name
            shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
    out.mkdir(parents=True, exist_ok=True)
