"""Publishing the index export on its own ref, outside `main` (M2-44, D39; spec §10.3).

`publish_export` commits exactly the export's files (the manifest, its shards and a
`* -text` `.gitattributes`, so no checkout converts line endings and breaks the sha256
values) to one ref - by default `refs/heads/agentic-index`. The commit is a child of the
ref's last export, so it changes only the shards that changed and a fetch transfers only
those (S9: a few KB a night); a night whose export is identical pushes nothing. After
`HISTORY` commits the ref starts again from one commit with no parent, so its history
stays small. The commit is built with git plumbing and a scratch index, as `agentic init
--repo` does: the checkout, its index and its branches never change.

It never writes `main`: `main`, `master`, the remote's default branch, the current
branch, tags and any ref outside `refs/heads/` and `refs/agentic/` are refused before
anything is built. Nor does it replace any other history: a ref that already exists is
moved only when its tip is an earlier index export, and the push is leased to the tip it
inspected (`--force-with-lease`), so a ref that moves meanwhile is never overwritten.
`--dry-run` builds the commit (loose objects only, no ref) and pushes nothing.

`archive_export` reads a fetched ref back into a folder for `agentic index import
--ref`: through `git archive`, accepting regular files and folders only.
"""

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from agentic.codeintel.export import MANIFEST, TOOL, read_export
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

DEFAULT_REF = "agentic-index"
#: Commits kept on the ref before it starts again from one (about a month of nights).
HISTORY = 30
NEVER = frozenset({"main", "master"})
ALLOWED_PREFIXES = ("refs/heads/", "refs/agentic/")
ATTRIBUTES = b"* -text\n"
#: The commit's identity when git has none configured (a CI runner).
BOT = {"name": "agentic index export", "email": "agentic-index-export@example.invalid"}
_GIT = ["git", "-c", "core.fsmonitor=false"]


def full_ref(ref: str) -> str:
    ref = ref.strip()
    return ref if ref.startswith("refs/") else f"refs/heads/{ref}"


def refusal(repo: Path, ref: str, remote: str) -> str | None:
    """Why the export may not be published to `ref` on `remote`, or None."""
    if not ref.startswith(ALLOWED_PREFIXES) or _run(repo, "check-ref-format", ref)[0] != 0:
        return f"{ref}: publish only to a branch (refs/heads/...) or a refs/agentic/... ref"
    name = ref.split("/", 2)[2]
    if ref.startswith("refs/heads/") and name in NEVER:
        return f"{ref}: the index export is never published to {name}"
    code, current = _run(repo, "symbolic-ref", "-q", "HEAD")
    if code == 0 and current.strip() == ref:
        return f"{ref} is the checked-out branch; publish the export to a ref of its own"
    code, listed = _run(repo, "ls-remote", "--symref", remote, "HEAD")
    if code != 0:
        return f"the default branch of {remote} could not be read, so {ref} cannot be checked"
    for line in listed.splitlines():
        if line.startswith("ref: ") and line[5:].split("\t", 1)[0].strip() == ref:
            return f"{ref} is the default branch of {remote}; the export stays outside it"
    return None


def publish_export(
    repo: Path, folder: Path, *, ref: str = DEFAULT_REF, remote: str = "origin", dry_run: bool
) -> dict:
    """Commit the export in `folder` on the last export `ref` holds on `remote` and push
    it there (not with `dry_run`, nor when the export is unchanged). Returns {ref, remote,
    commit, parent, files, unchanged, pushed, dry_run}."""
    target = full_ref(ref)
    if problem := refusal(repo, target, remote):
        raise CliError(problem, ExitCode.REFUSED, hint=f"use --ref {DEFAULT_REF} (the default)")
    manifest = read_export(folder)
    files = {MANIFEST: (folder / MANIFEST).read_bytes(), ".gitattributes": ATTRIBUTES}
    for entry in manifest["files"].values():
        files[entry["shard"]] = (folder / entry["shard"]).read_bytes()
    message = (
        f"agentic index export of {manifest['commit']}\n\n"
        f"{len(manifest['files'])} files; {manifest['tool']} {manifest['tool_version']}, "
        f"schema {manifest['schema_version']}, format {manifest['format']}\n"
    )
    tip = _published_tip(repo, target, remote)
    tree = _tree(repo, files)
    unchanged = tip is not None and _out(repo, "rev-parse", f"{tip}^{{tree}}") == tree
    parent = None
    if unchanged:
        commit = tip
    else:
        if tip is not None and int(_out(repo, "rev-list", "--count", tip)) < HISTORY:
            parent = tip
        commit = _commit(repo, tree, message, parent)
    pushed = not dry_run and not unchanged
    if pushed:
        lease = f"--force-with-lease={target}:{tip or ''}"
        code, said = _run(repo, "push", lease, "--porcelain", remote, f"{commit}:{target}")
        if code != 0:
            raise CliError(
                f"git push of the index export to {remote} {target} failed: {said.strip()}",
                hint="the job needs contents: write on this ref (see the workflow's header); "
                "if the ref moved meanwhile, run the job again",
            )
    return {
        "ref": target,
        "remote": remote,
        "commit": commit,
        "parent": parent,
        "files": len(manifest["files"]),
        "unchanged": unchanged,
        "pushed": pushed,
        "dry_run": dry_run,
    }


def _published_tip(repo: Path, target: str, remote: str) -> str | None:
    """The commit `target` holds on `remote`, fetched; None when there is none. Refuses a
    ref whose tip is not an earlier index export: publishing never replaces history."""
    code, listed = _run(repo, "ls-remote", remote, target)
    if code != 0:
        raise CliError(f"{remote} could not be read: {listed.strip()}")
    tips = [
        line.split("\t", 1)[0]
        for line in listed.splitlines()
        if "\t" in line and line.split("\t", 1)[1].strip() == target
    ]
    if not tips:
        return None
    tip = tips[0]
    code, said = _run(
        repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", remote, target
    )
    if code != 0:
        raise CliError(f"{target} could not be fetched from {remote}: {said.strip()}")
    if not _is_export(repo, tip):
        raise CliError(
            f"{target} on {remote} holds history that is not an index export; publishing "
            f"never replaces it",
            ExitCode.REFUSED,
            hint=f"use --ref {DEFAULT_REF} (the default), or a ref that does not exist yet",
        )
    return tip


def _is_export(repo: Path, commit: str) -> bool:
    """Whether `commit` is a commit whose tree is an index export (any format)."""
    if _run(repo, "cat-file", "-e", f"{commit}^{{commit}}")[0] != 0:
        return False
    code, text = _run(repo, "show", f"{commit}:{MANIFEST}")
    if code != 0:
        return False
    try:
        manifest = json.loads(text)
    except ValueError:
        return False
    return isinstance(manifest, dict) and manifest.get("tool") == TOOL and "format" in manifest


def archive_export(repo: Path, ref: str, into: Path) -> Path:
    """The export a fetched `ref` holds, extracted into the new folder `into`."""
    if ref.startswith("-"):
        raise CliError(f"{ref!r} is not a ref", ExitCode.USAGE)
    done = subprocess.run(
        [*_GIT, "-C", str(repo), "archive", "--format=tar", ref, "--"],
        capture_output=True,
    )
    if done.returncode != 0:
        error = done.stderr.decode("utf-8", "replace").strip()
        raise CliError(
            f"{ref} could not be read: {error}",
            ExitCode.INVALID,
            hint=f"git fetch origin {DEFAULT_REF}:refs/remotes/origin/{DEFAULT_REF} first",
        )
    into.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
        members = archive.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if not (member.isfile() or member.isdir()) or path.is_absolute() or ".." in path.parts:
                raise CliError(
                    f"{ref} holds {member.name}, which is not a plain file of an index export",
                    ExitCode.INVALID,
                )
        archive.extractall(into, members=members, filter="data")
    return into


def _tree(repo: Path, files: dict[str, bytes]) -> str:
    """The tree holding exactly `files`, written through a scratch index."""
    scratch = Path(_out(repo, "rev-parse", "--absolute-git-dir")) / "agentic-index-publish"
    env = {**os.environ, "GIT_INDEX_FILE": str(scratch)}
    scratch.unlink(missing_ok=True)
    try:
        for path, data in sorted(files.items()):
            blob = _out(repo, "hash-object", "-w", "--stdin", data=data)
            _out(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        return _out(repo, "write-tree", env=env)
    finally:
        scratch.unlink(missing_ok=True)


def _commit(repo: Path, tree: str, message: str, parent: str | None) -> str:
    env = {**os.environ}
    for kind in ("AUTHOR", "COMMITTER"):
        if not os.environ.get(f"GIT_{kind}_NAME") and not _run(repo, "config", "user.name")[1]:
            env[f"GIT_{kind}_NAME"] = BOT["name"]
        if not os.environ.get(f"GIT_{kind}_EMAIL") and not _run(repo, "config", "user.email")[1]:
            env[f"GIT_{kind}_EMAIL"] = BOT["email"]
    parents = ["-p", parent] if parent else []
    return _out(repo, "commit-tree", tree, *parents, "-m", message, env=env)


def _run(repo: Path, *args: str) -> tuple[int, str]:
    done = subprocess.run(
        [*_GIT, "-C", str(repo), *args], capture_output=True, text=True, errors="replace"
    )
    return done.returncode, done.stdout + done.stderr if done.returncode else done.stdout


def _out(repo: Path, *args: str, data: bytes | None = None, env: dict | None = None) -> str:
    done = subprocess.run([*_GIT, "-C", str(repo), *args], input=data, capture_output=True, env=env)
    if done.returncode != 0:
        error = done.stderr.decode("utf-8", "replace").strip()
        raise CliError(f"git {args[0]} failed: {error or done.returncode}")
    return done.stdout.decode("utf-8").strip()
