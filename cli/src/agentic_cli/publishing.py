"""Publishing a run (M1-58): push its branch, open its pull request, watch its CI.

Zyroflo's release flow (`zyroflo.sdlc.release`, P8), followed step by step:

  1. **through the wall** - each of the run's trees is pushed only when Zyroflo's push
     wall (`Guard.push`: the approved remote, the run's own `sdlc/<run>` branch) allows
     it; a refusal stops before anything leaves the machine;
  2. **push** - plain `git push` with the person's own git credentials, never forced;
  3. **open** - the pull request on the repository's default branch, its body M1-45's
     `pr_body` (the run, its cost so far and where its logs are, every handoff with its
     digest, the approvals, the AGENTS.md additions agents proposed), masked by the
     commons redactor (the title too) and set between `<!-- agentic:run -->` markers.
     Publishing again updates only that part of the open pull request's body, keeping
     what a person wrote around it, instead of opening another;
  4. **watch** - `watch_ci` reads every check until none is still running and judges
     them with Zyroflo's verdict (`sdlc_commons.release`): only green passes (a skipped
     check settles the watch but is not a pass). No checks at all is answered `none`
     only after a grace period, because a host registers a push's checks a little after
     the push. A failed read that may pass (network, 5xx, rate limit) is tried again.

Nothing here merges: a person does. What was published is recorded in the run folder
(`pr.json`), and the last CI verdict next to it (`ci.json`); neither holds the token.

Links: with a control plane (`control_plane_url` in the user config, or
`AGENTIC_CONTROL_PLANE_URL`), the body links the run to its summary there
(`<cp>/v1/runs/<run_id>`, ND-11); handoffs and step logs are named by their paths in the
starter's run folder either way, as code (DF-166), because their bodies are not uploaded.
"""

import json
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep

from agentic.engine.state import RunState
from agentic.hosting import CheckRun, HostingError
from agentic.hosting.git import push_branch
from agentic.memory.agents_md import proposals
from agentic.orgapi import API_PREFIX
from agentic.telemetry.trailers import pr_body
from agentic_cli import control_plane
from agentic_cli.driving import spent
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import Connection, connect
from agentic_cli.workspace import RUNS, Workspace
from sdlc_commons.guards import Guard, GuardError
from sdlc_commons.redaction import redactor
from sdlc_commons.release import NONE, PENDING, ci_verdict
from sdlc_commons.worktrees import RunWorktrees, Tree, WorktreeError

PR_FILE = "pr.json"
CI_FILE = "ci.json"
SCHEMA_VERSION = 1
REMOTE = "origin"
#: A host registers a push's checks shortly after it; "no checks" before this is no answer.
NONE_GRACE_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_INTERVAL_SECONDS = 15
#: A watch survives this many failed reads in a row (a proxy blip, a 502, a rate limit).
MAX_TRANSIENT_FAILURES = 5
TITLE_LIMIT = 100
#: Around the generated part of a pull request's description: publishing again replaces
#: only what is between them (HTML comments: invisible on the rendered page).
BODY_BEGIN = "<!-- agentic:run -->"
BODY_END = "<!-- /agentic:run -->"


def publish_run(ws: Workspace, store_root: Path, state: RunState, kind: str | None = None) -> dict:
    """Push the run's branch(es) and open or update each pull request; returns (and
    records in `pr.json`) what was published. `kind` - a provider kind such as "github" -
    refuses a repository on another kind of host (the `github.open_pr` action)."""
    trees = _trees(ws.repo, state.run_id)
    texts = _handoff_texts(store_root / state.run_id, state)
    found = proposals(texts, ws.definitions().handoffs) if texts else []
    published = []
    for component, tree in sorted(trees.items()):
        _wall(ws.repo, state.run_id, tree)
        with connect(tree.path, ws.settings, remote=REMOTE) as conn, conn.errors():
            if kind is not None and conn.kind != kind:
                raise CliError(
                    f"this step opens a {kind} pull request, but {conn.repo.host} is a "
                    f"{conn.kind} host",
                    ExitCode.REFUSED,
                    hint="use `action: open_pr`, which opens it on whatever host origin is",
                )
            push_branch(tree.path, conn.remote, tree.branch, secrets=[conn.token])
            redact = redactor(extra=[conn.token])
            block = _marked(redact(_body(ws, state, found)))
            provider, repo = conn.provider, conn.repo
            existing = provider.find_pull_request(repo, tree.branch)
            if existing is not None:  # only the generated part changes (DF-296)
                body = merge_body(existing.body, block)
                pull = provider.update_pull_request(repo, existing.number, body=body)
            else:
                base = provider.default_branch(repo)
                title = _title(state, redact)  # a run input may hold a secret (DF-295)
                pull = provider.open_pull_request(
                    repo, head=tree.branch, base=base, title=title, body=block
                )
            published.append(
                {
                    "component": component,
                    "kind": conn.kind,
                    "host": repo.host,
                    "repo": repo.slug,
                    "number": pull.number,
                    "url": pull.url,
                    "branch": tree.branch,
                    "base": pull.base,
                    "head_sha": pull.head_sha,
                    "created": existing is None,
                }
            )
    record = {
        "schema_version": SCHEMA_VERSION,
        "run_id": state.run_id,
        "at": _now(),
        "pull_requests": published,
    }
    _write_json(store_root / state.run_id / PR_FILE, record)
    return record


def publish_action(ws: Workspace, store_root: Path, kind: str | None) -> Callable:
    """The engine action (`github.open_pr`, `open_pr`): publish, and say what was done."""

    def action(state: RunState) -> str:
        try:
            record = publish_run(ws, store_root, state, kind=kind)
        except CliError as error:
            # the engine keeps only the message as the pause reason: what to do goes in it
            raise CliError(with_hint(error), error.code, hint=error.hint) from error
        return "; ".join(
            f"{'opened' if p['created'] else 'updated'} {p['url']}" for p in record["pull_requests"]
        )

    return action


def publish_check(ws: Workspace, kind: str | None) -> str | None:
    """Why a publish step of this workspace would be refused - origin not on a known
    host, no token, a host of another kind - or None. Checked at preflight, so a run
    does not spend its budget on agents only to fail at the last step."""
    try:
        with connect(ws.repo, ws.settings, remote=REMOTE) as conn:
            if kind is not None and conn.kind != kind:
                return (
                    f"this step opens a {kind} pull request, but {conn.repo.host} is a "
                    f"{conn.kind} host (use `action: open_pr`)"
                )
    except CliError as error:
        return with_hint(error)
    return None


def with_hint(error: CliError) -> str:
    """The error's message with its hint, for places that keep only one line."""
    return f"{str(error).rstrip('.')}. {error.hint}" if error.hint else str(error)


def published(store_root: Path, run_id: str) -> dict:
    """What `publish_run` recorded for the run; refused when it never published."""
    path = store_root / run_id / PR_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(
            f"run {run_id} has no pull request yet",
            ExitCode.REFUSED,
            hint=f"open it with `agentic pr open {run_id}` once the run has completed",
        ) from None
    except (OSError, ValueError) as error:
        raise CliError(f"{path} cannot be read: {error}", ExitCode.ERROR) from error
    return record


def run_checks(
    ws: Workspace,
    store_root: Path,
    run_id: str,
    *,
    watch: bool,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    interval: float = DEFAULT_INTERVAL_SECONDS,
) -> dict:
    """The CI verdict over every pull request the run opened (read once, or watched until
    it settles); recorded in `ci.json`."""
    record = published(store_root, run_id)
    try:
        trees = RunWorktrees(ws.repo, run_id).trees()
    except WorktreeError:
        trees = {}
    connections: list[tuple[dict, Connection]] = []
    try:
        for entry in record.get("pull_requests") or []:
            tree = trees.get(entry.get("component", ""))
            where = tree.path if tree is not None and tree.path.is_dir() else ws.repo
            connections.append((entry, connect(where, ws.settings, remote=REMOTE)))
        commits: list[dict] = []

        def read() -> list[CheckRun]:
            commits.clear()
            rows: list[CheckRun] = []
            for entry, conn in connections:
                with conn.errors(keep_transient=True):  # `watch_ci` may try again
                    current = conn.provider.find_pull_request(conn.repo, entry["branch"])
                    sha = current.head_sha if current is not None else entry["head_sha"]
                    commits.append({"component": entry["component"], "sha": sha})
                    rows += conn.provider.checks(conn.repo, sha)
            return rows

        try:
            if watch:
                verdict = watch_ci(read, timeout=timeout, interval=interval)
            else:  # one read: nothing waited, so nothing timed out
                verdict = {
                    **watch_ci(read, timeout=0, interval=0, none_grace=0),
                    "timed_out": False,
                }
        except HostingError as error:
            raise CliError(
                str(error),
                ExitCode.ERROR,
                hint="the host did not answer properly; try again later "
                "(`agentic pr checks --watch` rides out short outages)",
            ) from error
    finally:
        for _, conn in connections:
            conn.close()
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "at": _now(),
        "commits": list(commits),
        **verdict,
    }
    _write_json(store_root / run_id / CI_FILE, result)
    return result


def watch_ci(
    read: Callable[[], list[CheckRun]],
    *,
    timeout: float,
    interval: float,
    none_grace: float = NONE_GRACE_SECONDS,
) -> dict:
    """Read the checks until they settle (no check still running, and checks present or
    the grace period over) or `timeout` passes; Zyroflo's verdict, plus `timed_out` and
    the rows read last. A skipped check is not a pass, but nothing is left to wait for.

    A read that fails with a transient `HostingError` (the network, a 5xx, a rate limit)
    is tried again after `interval` (or the host's `Retry-After`, if longer), up to
    `MAX_TRANSIENT_FAILURES` in a row and never past `timeout`; then, and on any other
    error, the error is raised."""
    started = monotonic()
    failures = 0
    while True:
        try:
            rows = read()
        except HostingError as error:
            failures += 1
            spent = monotonic() - started
            if not error.transient or failures >= MAX_TRANSIENT_FAILURES or spent >= timeout:
                raise
            sleep(max(interval, min(error.retry_after or 0, timeout - spent)))
            continue
        failures = 0
        verdict = ci_verdict(asdict(row) for row in rows)
        waited = monotonic() - started
        state = verdict["state"]
        settled = (
            state not in (PENDING, NONE)
            or (state == PENDING and not verdict["running"])
            or (state == NONE and waited >= none_grace)
        )
        if settled or waited >= timeout:
            return {**verdict, "timed_out": not settled, "rows": [asdict(r) for r in rows]}
        sleep(interval)


def control_plane_url(ws: Workspace) -> str | None:
    return control_plane.control_plane_url(ws.env, ws.settings)


def run_link(ws: Workspace, run_id: str) -> str | None:
    """Where a person sees the run: the control plane's run summary until a web UI exists
    (ND-11), or None without a control plane."""
    base = control_plane_url(ws)
    return f"{base}{API_PREFIX}/runs/{run_id}" if base else None


# ── helpers ──────────────────────────────────────────────────────────────────


def _trees(repo: Path, run_id: str) -> dict[str, Tree]:
    try:
        trees = RunWorktrees(repo, run_id).trees()
    except WorktreeError as error:
        raise CliError(f"the run's branch cannot be found: {error}", ExitCode.ERROR) from error
    if not trees:
        raise CliError(
            f"run {run_id} has no branch to publish: none of its steps made a tree",
            ExitCode.REFUSED,
        )
    in_place = sorted(name for name, tree in trees.items() if tree.mode != "worktree")
    if in_place:
        raise CliError(
            f"component(s) {', '.join(in_place)} have no run branch (worked in place), so "
            "there is nothing to push",
            ExitCode.REFUSED,
        )
    return trees


def _wall(repo: Path, run_id: str, tree: Tree) -> None:
    """Zyroflo's push wall, before anything leaves the machine."""
    try:
        blocks = Guard(repo, run_id).push(REMOTE, tree.branch)
    except GuardError as error:
        raise CliError(f"the push wall cannot be read: {error}", ExitCode.ERROR) from error
    if blocks:
        raise CliError(
            f"the push wall refused {tree.branch} to {REMOTE}: "
            + "; ".join(block.why for block in blocks),
            ExitCode.REFUSED,
            hint="the repository's .sdlc policy decides where runs may publish",
        )


def _handoff_texts(folder: Path, state: RunState) -> dict[str, str]:
    texts = {}
    for record in state.history:
        rel = record.handoff
        if not rel or rel.endswith(".invalid.md") or rel in texts:
            continue
        try:
            texts[rel] = (folder / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable: no proposals from it, the link still shows
    return texts


def _body(ws: Workspace, state: RunState, found: list[tuple[str, str]]) -> str:
    folder = f"{RUNS.as_posix()}/{state.run_id}"
    # handoff bodies and step logs are not uploaded, so they stay named in the starter's
    # run folder
    logs = f"`agentic logs {state.run_id}` in the starter's checkout"
    return pr_body(
        state,
        run_link=run_link(ws, state.run_id),
        handoff_link=lambda rel: None,
        proposals=found,
        run_folder=folder,
        cost_usd=spent(state),
        logs=logs,
    )


def merge_body(current: str | None, block: str) -> str:
    """The pull request's description with the generated `block` put in place of the
    earlier one, keeping whatever a person wrote around it (DF-296). With no markers in
    `current` (a person removed them), the block is added at the end - nothing of theirs
    is lost; when the host did not say (None), the block is the whole description."""
    if current is None or not current.strip():
        return block
    start, end = current.find(BODY_BEGIN), current.find(BODY_END)
    if start == -1 or end == -1 or end < start:
        return f"{current.rstrip()}\n\n{block}"
    after = current[end + len(BODY_END) :].removeprefix("\r\n").removeprefix("\n")
    return f"{current[:start]}{block}{after}"


def _marked(body: str) -> str:
    return f"{BODY_BEGIN}\n{body.rstrip()}\n{BODY_END}\n"


def _title(state: RunState, redact: Callable[[str], str]) -> str:
    """The PR title, redacted whole before it is cut: a secret straddling the limit would
    no longer be a whole value for the redactor to match (review F1)."""
    request = next((v for v in state.inputs.values() if str(v).strip()), "")
    # its first line: a request read from a tracker carries the ticket after a header line
    # (M2-50), and the fenced ticket text is no title
    first = " ".join(str(request).strip().splitlines()[0].split()) if str(request).strip() else ""
    title = f"{state.pipeline}: {first}" if first else f"{state.pipeline} run {state.run_id}"
    title = redact(title)
    return title if len(title) <= TITLE_LIMIT else title[: TITLE_LIMIT - 3].rstrip() + "..."


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
