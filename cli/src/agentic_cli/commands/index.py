"""The opt-in code-intelligence index export (M2-44, D39; spec §10.3):

    agentic index export --out DIR [--repo PATH]          build once (CI), write the export
    agentic index import [DIR | --ref REF] [--repo PATH]  start from a published export
    agentic index publish DIR [--ref agentic-index] [--remote origin] [--dry-run]

A repo whose full local build is too slow has the nightly job (the workflow `agentic
init --repo` proposes when org policy sets `index_export_workflow`) run `export` and
publish the result outside `main`: as a workflow artifact, or with `publish` on its own
ref. A developer downloads it (`gh run download -n agentic-index-export`, or `git fetch
origin agentic-index`) and runs `import`: the export is checked, pinned to this
machine's code-review-graph and schema version, kept in `.agentic/local/index-export/`
- where every run's index starts from it - and the local index is caught up with an
incremental update. None of these commands needs a login or org policy.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from agentic.codeintel.export import (
    MANIFEST,
    ExportError,
    ExportMismatch,
    check_tool,
    export_index,
    installed_tool_version,
    out_problem,
    read_export,
)
from agentic.codeintel.index import LocalIndex, available
from agentic.codeintel.interface import CodeIntelError
from agentic_cli.codeintel import INDEX_EXPORT_REL, LOCAL_INDEX_REL
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.workspace import find_repo

RepoOption = Annotated[
    Path | None, typer.Option("--repo", help="The repository (default: the current one).")
]
_GIT = ["git", "-c", "core.fsmonitor=false"]


def register(app: typer.Typer) -> None:
    group = typer.Typer(
        help="The code-intelligence index export for repos too slow to index locally.",
        no_args_is_help=True,
    )
    app.add_typer(group, name="index")

    @group.command("export")
    def export(
        ctx: typer.Context,
        out: Annotated[
            Path, typer.Option("--out", help="A new or empty folder, or an earlier export.")
        ],
        repo: RepoOption = None,
    ) -> None:
        """Build the index of the checked-out commit once and write its export."""
        output: Output = ctx.obj
        root = find_repo(repo or Path.cwd())
        if not available():
            raise CliError(
                "the codeintel extra (code-review-graph) is not installed",
                ExitCode.ERROR,
                hint="install agentic-core[codeintel] where the export runs",
            )
        if problem := out_problem(out):
            raise CliError(problem, ExitCode.REFUSED, hint="--out names a folder it may replace")
        if _git(root, "status", "--porcelain"):
            raise CliError(
                f"{root} has uncommitted changes; the export is of a commit",
                ExitCode.REFUSED,
                hint="commit or stash them, or export from a clean checkout (the CI job does)",
            )
        output.progress(f"building the index of {root} ...")
        with tempfile.TemporaryDirectory(prefix="agentic-index-", ignore_cleanup_errors=True) as d:
            data = Path(d)  # outside the tree: the checkout is never written to
            index = LocalIndex(root, data / "index", home_dir=data / "home")
            try:
                index.ensure()
                manifest = export_index(index.db, root, out)
            except CodeIntelError as error:
                raise CliError(str(error), ExitCode.ERROR) from error
        result = {
            "out": str(out),
            "commit": manifest["commit"],
            "files": len(manifest["files"]),
            "nodes": manifest["nodes"],
            "edges": manifest["edges"],
            "tool_version": manifest["tool_version"],
            "schema_version": manifest["schema_version"],
        }
        output.result(
            result,
            lambda c: c.print(
                f"exported {result['files']} files ({result['nodes']} symbols, "
                f"{result['edges']} relations) of {result['commit'][:12]} to {out}",
                markup=False,
            ),
        )

    @group.command("import")
    def import_(
        ctx: typer.Context,
        folder: Annotated[
            Path | None, typer.Argument(metavar="[DIR]", help="An export folder.")
        ] = None,
        ref: Annotated[
            str | None,
            typer.Option("--ref", help="A fetched ref holding the export (origin/agentic-index)."),
        ] = None,
        repo: RepoOption = None,
    ) -> None:
        """Start the local index (and every run's) from a published export."""
        output: Output = ctx.obj
        if (folder is None) == (ref is None):
            raise CliError("give an export folder or --ref, not both", ExitCode.USAGE)
        root = find_repo(repo or Path.cwd())
        if not available():
            raise CliError(
                "the codeintel extra (code-review-graph) is not installed",
                ExitCode.ERROR,
                hint="install agentic-core[codeintel]",
            )
        with tempfile.TemporaryDirectory(prefix="agentic-index-", ignore_cleanup_errors=True) as d:
            source = folder
            if ref is not None:
                from agentic_cli.index_publish import archive_export

                source = archive_export(root, ref, Path(d) / "export")
            manifest = _checked(source)
            local = _local(root)
            seed = _place_seed(source, local / INDEX_EXPORT_REL.name)
        index = LocalIndex(root, local / "index", home_dir=local / "index" / "home", seed=seed)
        output.progress(f"importing the index of {manifest['commit'][:12]} ...")
        try:
            index.load_export(seed)
            index.ensure()  # an incremental update from the export's commit to HEAD
            recorded = index.recorded_commit()
        except ExportMismatch as error:
            raise CliError(str(error), ExitCode.REFUSED) from error
        except CodeIntelError as error:
            raise CliError(str(error), ExitCode.ERROR) from error
        result = {
            "commit": manifest["commit"],
            "files": len(manifest["files"]),
            "seed": str(seed),
            "index": str(index.data_dir),
            "recorded_commit": recorded,
        }
        output.result(
            result,
            lambda c: c.print(
                f"imported the index of {manifest['commit'][:12]} ({result['files']} files); "
                f"the local index is now at {str(recorded)[:12]}",
                markup=False,
            ),
        )

    @group.command("publish")
    def publish(
        ctx: typer.Context,
        folder: Annotated[Path, typer.Argument(metavar="DIR", help="An export folder.")],
        ref: Annotated[
            str, typer.Option("--ref", help="The ref to publish to (never the default branch).")
        ] = "agentic-index",
        remote: Annotated[str, typer.Option("--remote", help="The remote to push to.")] = "origin",
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Build the commit, push nothing.")
        ] = False,
        repo: RepoOption = None,
    ) -> None:
        """Publish an export on its own ref, outside main (only the shards that changed)."""
        from agentic_cli.index_publish import publish_export

        output: Output = ctx.obj
        root = find_repo(repo or Path.cwd())
        _checked(folder, pinned=False)
        result = publish_export(root, folder, ref=ref, remote=remote, dry_run=dry_run)
        if result["unchanged"]:
            verb = "unchanged, nothing to push:"
        else:
            verb = "would push" if dry_run else "pushed"
        output.result(
            result,
            lambda c: c.print(
                f"{verb} {result['files']} files as {result['commit'][:12]} to "
                f"{remote} {result['ref']}",
                markup=False,
            ),
        )


def _checked(folder: Path, *, pinned: bool = True) -> dict:
    """The export's manifest; damaged -> INVALID, another version -> REFUSED."""
    try:
        manifest = read_export(folder)
        if pinned:
            check_tool(manifest, installed_tool_version())
    except ExportMismatch as error:
        raise CliError(
            str(error),
            ExitCode.REFUSED,
            hint="the export job and developers must use the org's pinned agentic version",
        ) from error
    except ExportError as error:
        raise CliError(str(error), ExitCode.INVALID) from error
    return manifest


def _local(repo: Path) -> Path:
    """`.agentic/local`, git-ignored; refuses links (a repo can commit them there)."""
    local = repo / LOCAL_INDEX_REL.parent
    for path in (repo / ".agentic", local, local / INDEX_EXPORT_REL.name, local / "index"):
        if path.is_symlink() or path.is_junction():
            raise CliError(
                f"{path} is a link; the index is written only into real folders",
                ExitCode.REFUSED,
                hint="remove the link and run the import again",
            )
    local.mkdir(parents=True, exist_ok=True)
    ignore = local / ".gitignore"
    if not ignore.is_file():
        ignore.write_text("*\n", encoding="utf-8", newline="\n")
    return local


def _place_seed(source: Path, seed: Path) -> Path:
    """Copy the export's own files (the manifest and its shards, nothing else) beside
    `seed`, check the copy, then put it in place of the old seed."""
    manifest = read_export(source)
    staged = Path(tempfile.mkdtemp(prefix=".index-export-", dir=seed.parent))
    try:
        for entry in manifest["files"].values():
            target = staged / entry["shard"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / entry["shard"], target)
        shutil.copyfile(source / MANIFEST, staged / MANIFEST)
        read_export(staged)
        if seed.exists():
            shutil.rmtree(seed)
        staged.rename(seed)
    except (OSError, ExportError) as error:
        shutil.rmtree(staged, ignore_errors=True)
        raise CliError(f"the export could not be kept in {seed}: {error}") from error
    return seed


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run([*_GIT, "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise CliError(f"git {args[0]} failed in {repo}: {done.stderr.strip()}")
    return done.stdout.strip()
