"""Promoting a personal skill (M2-46; spec §24 "Custom skills" step 5, D30).

`submit` proposes the skill to the owner of a scope:

  * **repo** - a pull request on the repository's own host adding `.agentic/skills/<name>/`
    (the repo maintainers approve it through CODEOWNERS);
  * **team** / **org** - a pull request on the policy repo adding the skill under
    `teams/<team>/skills/internal/<name>/` or `skills/internal/<name>/`, with its entry
    (pinned by sha256) in that catalog (the team's leads or the org admins approve it,
    CODEOWNERS, D33).

The branch is built with git plumbing (`repo_setup.commit_on_branch`): the checkout, its
index and its current branch never change. The pull request carries the skill's eval
report (`agentic skills eval` of exactly this version: the sha256 must match) and a
machine-readable submission block that `review` reads. The submitter's CLI records the
submission in `~/.agentic/skills-submitted/<name>.json`.

`review` lists the open submissions of a scope; `approve` and `decline` post a review
after an RBAC check through the control plane (`/v1/rbac/check`, the authorizer
`init --repo` uses): the scope's permission is

    repo  agent.approve.repo     (repo maintainers, team leads of the repo's team)
    team  agent.publish.team     (that team's leads)
    org   skills.approve         (org admins)

Without an org there is no RBAC (standalone mode): the command says so and proceeds, as
gate approvals do; the host's code owners and branch protection still decide the merge.
In org mode without a reachable control plane, approval is refused.

`status` reads each recorded submission's pull request; once it is merged the shared copy
exists, so the personal copy is retired: moved (never deleted) to
`~/.agentic/skills-retired/<name>-<time>/`. A declined submission keeps it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic.hosting import RepoRef
from agentic.hosting.base import ReviewHost
from agentic.hosting.git import push_branch
from agentic.policy.repos import normalise_remote
from agentic.skills import SkillsError
from agentic.skills.admission import Rejection
from agentic.skills.authoring import (
    INTERNAL,
    PROMOTION_BRANCH,
    Scope,
    add_catalog_entry,
    catalog_entry,
    check_name,
    check_skill,
    promotion_paths,
    skill_files_for_commit,
    skill_sha,
)
from agentic_cli import control_plane, repo_setup
from agentic_cli.errors import CliError
from agentic_cli.evaluation import SKILL_EVAL_FILE, skill_eval_markdown
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import Connection, connect, connect_to

SUBMITTED = "skills-submitted"
RETIRED = "skills-retired"
EVALS = "skills-eval"
BRANCH_PREFIX = PROMOTION_BRANCH.format(name="")
PERMISSIONS: dict[str, str] = {
    "repo": "agent.approve.repo",
    "team": "agent.publish.team",
    "org": "skills.approve",
}
BLOCK = re.compile(r"<!-- agentic:skill-submission (\{.*?\}) -->", re.DOTALL)
#: A host caps a description (GitHub: 65 536 characters); the report is cut well before.
MAX_BODY = 60_000
_GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false"]


# ── the skill and its eval ───────────────────────────────────────────────────


def personal_folder(home: Path, name: str) -> Path:
    try:
        check_name(name)
    except SkillsError as error:
        raise CliError(str(error), ExitCode.USAGE) from error
    folder = home / "skills" / name
    if not folder.is_dir():
        raise CliError(
            f"no personal skill {name!r} in {home / 'skills'}",
            ExitCode.INVALID,
            hint=f"create it with `agentic skills new {name}`",
        )
    return folder


def problems_as_dicts(found: list[Rejection]) -> list[dict[str, str]]:
    unique = dict.fromkeys((r.check, r.path, r.reason) for r in found)
    return [{"check": c, "path": p, "reason": r} for c, p, r in unique]


def latest_eval(home: Path, name: str, sha256: str, given: Path | None = None) -> dict:
    """The skill-eval summary of exactly this skill version: from `given` (a folder
    `skills eval` wrote), else the newest under `~/.agentic/skills-eval/<name>/`."""
    candidates = [given] if given is not None else _eval_folders(home / EVALS / name)
    seen_other = False
    for folder in candidates:
        path = folder / SKILL_EVAL_FILE
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if given is not None:
                raise CliError(f"{path} is not a skills eval summary", ExitCode.INVALID) from None
            continue
        if summary.get("skill") != name or summary.get("sha256") != sha256:
            seen_other = True
            continue
        if summary.get("verdict") != "MEASURED":
            raise CliError(
                f"the eval of {name} did not measure ({summary.get('verdict')})",
                ExitCode.REFUSED,
                hint=f"see {folder}; run `agentic skills eval {name}` again",
            )
        summary["folder"] = str(folder)
        return summary
    why = "of an older version of the skill" if seen_other else "found"
    raise CliError(
        f"no eval of this version of {name} {why}: a submission carries its eval report",
        ExitCode.REFUSED,
        hint=f"run `agentic skills eval {name} --agent <agent>` first",
    )


def _eval_folders(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    folders = [p for p in root.iterdir() if (p / SKILL_EVAL_FILE).is_file()]
    return sorted(folders, key=lambda p: (p / SKILL_EVAL_FILE).stat().st_mtime, reverse=True)


# ── where a submission goes ──────────────────────────────────────────────────


@contextmanager
def policy_checkout(settings: Mapping[str, Any], given: Path | None) -> Iterator[Path]:
    """A checkout of the policy repo to build the branch in: `given`, or the user
    config's `policy_checkout`, else a shallow clone of `policy_repo` in a scratch folder
    (removed afterwards; the branch has been pushed by then)."""
    named = given or (
        Path(str(settings["policy_checkout"])) if settings.get("policy_checkout") else None
    )
    if named is not None:
        if not (named / ".git").exists():
            raise CliError(f"{named} is not a git checkout of the policy repo", ExitCode.INVALID)
        yield named
        return
    source = str(settings.get("policy_repo") or "").strip()
    if not source:
        raise CliError(
            "team and org skills go to the policy repo, and none is configured",
            ExitCode.REFUSED,
            hint="pass --policy-checkout <clone>, or set policy_repo in ~/.agentic/config.yaml",
        )
    with tempfile.TemporaryDirectory(prefix="agentic-skill-") as scratch:
        target = Path(scratch) / "policy"
        done = subprocess.run(
            [*_GIT, "clone", "-q", "--depth", "1", "--", source, str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
        if done.returncode != 0:
            tail = (done.stderr.strip().splitlines() or ["git clone failed"])[-1]
            raise CliError(f"could not clone the policy repo: {tail}", ExitCode.ERROR)
        yield target


def head_text(repo: Path, path: str) -> str | None:
    """`path` as committed at HEAD (LF text), or None when it is not there."""
    done = subprocess.run(
        [*_GIT, "-C", str(repo), "cat-file", "-p", f"HEAD:{path}"], capture_output=True
    )
    return (
        done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        if done.returncode == 0
        else None
    )


def head_has(repo: Path, path: str) -> bool:
    done = subprocess.run(
        [*_GIT, "-C", str(repo), "ls-tree", "-z", "--name-only", "HEAD", "--", path],
        capture_output=True,
    )
    return bool(done.stdout.strip(b"\0"))


@dataclass(frozen=True)
class Submission:
    name: str
    scope: Scope
    team: str | None
    sha256: str
    submitter: str
    eval: dict


def build_files(target: Path, sub: Submission, source: Path) -> tuple[dict[str, bytes], list[str]]:
    """Every file of the promotion branch, and their paths for the description."""
    folder_rel, catalog_rel = promotion_paths(sub.scope, sub.name, sub.team)
    if head_has(target, folder_rel):
        raise CliError(
            f"a shared skill already exists at {folder_rel}",
            ExitCode.REFUSED,
            hint="rename your skill, or propose a change to the shared one",
        )
    files = {f"{folder_rel}/{rel}": data for rel, data in skill_files_for_commit(source).items()}
    if catalog_rel is not None:
        entry = catalog_entry(sub.name, sub.sha256, owner=sub.submitter, path=folder_rel)
        try:
            text = add_catalog_entry(
                head_text(target, catalog_rel), f"{INTERNAL}/{sub.name}", entry
            )
        except SkillsError as error:
            raise CliError(f"{catalog_rel}: {error}", ExitCode.REFUSED) from error
        files[catalog_rel] = text.encode("utf-8")
    return files, sorted(files)


def pr_title(sub: Submission) -> str:
    where = f"team {sub.team}" if sub.scope == "team" else sub.scope
    return f"Promote skill {sub.name} to {where}"


def pr_body(sub: Submission, paths: list[str]) -> str:
    block = {
        "name": sub.name,
        "scope": sub.scope,
        "team": sub.team,
        "sha256": sub.sha256,
        "submitter": sub.submitter,
        "agent": sub.eval.get("agent"),
        "report_id": sub.eval.get("report_id"),
        "verdict": sub.eval.get("verdict"),
    }
    lines = [
        f"## Promote the skill `{sub.name}`",
        "",
        f"Submitted by {sub.submitter} with `agentic skills submit`. The scope owner reviews it",
        "(`agentic skills review`), and a person merges it through the code owners.",
        "",
        *(f"- `{path}`" for path in paths),
        "",
        skill_eval_markdown(sub.eval).rstrip(),
        "",
        f"<!-- agentic:skill-submission {json.dumps(block, sort_keys=True)} -->",
    ]
    body = "\n".join(lines) + "\n"
    return body if len(body) <= MAX_BODY else body[: MAX_BODY - 200] + "\n\n(cut)\n"


def submission_of(body: str | None) -> dict | None:
    """The submission block of a pull request's description, or None."""
    found = BLOCK.findall(body or "")
    if not found:
        return None
    try:
        data = json.loads(found[-1])
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("name") else None


def open_pull_request(
    target: Path, branch: str, sub: Submission, paths: list[str], settings: Mapping[str, Any]
) -> tuple[dict, Connection]:
    conn = connect(target, settings)
    with conn.errors():
        push_branch(target, conn.remote, branch, secrets=[conn.token])
        provider = conn.provider
        existing = provider.find_pull_request(conn.repo, branch)
        if existing is not None:
            pull, created = existing, False
        else:
            pull = provider.open_pull_request(
                conn.repo,
                head=branch,
                base=provider.default_branch(conn.repo),
                title=pr_title(sub),
                body=pr_body(sub, paths),
            )
            created = True
    return {"number": pull.number, "url": pull.url, "created": created}, conn


def record_submission(home: Path, sub: Submission, repo: RepoRef, pull: dict, branch: str) -> Path:
    folder = home / SUBMITTED
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "name": sub.name,
        "scope": sub.scope,
        "team": sub.team,
        "sha256": sub.sha256,
        "repo": {"host": repo.host, "owner": repo.owner, "name": repo.name},
        "number": pull["number"],
        "url": pull["url"],
        "branch": branch,
        "report_id": sub.eval.get("report_id"),
        "submitted_at": _now(),
        "state": "open",
    }
    path = folder / f"{sub.name}.json"
    _write_json(path, record)
    return path


# ── review ───────────────────────────────────────────────────────────────────


def review_host(conn: Connection) -> ReviewHost:
    if not isinstance(conn.provider, ReviewHost):
        raise CliError(
            f"the {conn.kind} adapter cannot list or review pull requests yet", ExitCode.REFUSED
        )
    return conn.provider


def pending(conn: Connection, scope: str | None, team: str | None) -> list[dict]:
    host = review_host(conn)
    rows = []
    with conn.errors():
        pulls = host.list_pull_requests(conn.repo, head_prefix=BRANCH_PREFIX)
    for pull in pulls:
        block = submission_of(pull.body)
        if block is None:
            continue
        if scope is not None and block.get("scope") != scope:
            continue
        if team is not None and block.get("team") != team:
            continue
        rows.append(
            {
                "number": pull.number,
                "url": pull.url,
                "skill": block.get("name"),
                "scope": block.get("scope"),
                "team": block.get("team"),
                "submitter": block.get("submitter") or pull.author,
                "sha256": block.get("sha256"),
                "agent": block.get("agent"),
                "verdict": block.get("verdict"),
            }
        )
    return rows


def authorize(
    scope: str,
    team: str | None,
    repo_key: str | None,
    settings: Mapping[str, Any],
    env: Mapping[str, str],
    home: Path,
    warn: Callable[[str], None],
) -> str:
    """The permission the decision was made under; CliError (exit 2) when the person may
    not decide for `scope`."""
    permission = PERMISSIONS[scope]
    if not settings.get("org"):
        warn(
            "standalone mode has no RBAC: anyone may review here; the host's code owners "
            "and branch protection still decide the merge"
        )
        return permission
    try:
        client = control_plane.client_for(env, settings, home)
    except control_plane.ControlPlaneError as error:
        raise CliError(
            f"approving a {scope} skill is checked by the control plane: {error}",
            ExitCode.REFUSED,
            hint=error.hint,
        ) from None
    if client is None:
        raise CliError(
            f"approving a {scope} skill needs the control plane to say who may (org mode)",
            ExitCode.REFUSED,
            hint="set control_plane_url in ~/.agentic/config.yaml and run `agentic login`",
        )
    try:
        with client:
            allowed = client.check(permission, team=team, repo=repo_key).allowed
    except control_plane.ControlPlaneError as error:
        raise CliError(
            f"the control plane could not check {permission}: {error}",
            ExitCode.REFUSED,
            hint=error.hint,
        ) from None
    if not allowed:
        owner = {"repo": "a maintainer of this repo", "team": f"a lead of team {team}"}
        raise CliError(
            f"you may not approve {scope} skills here: {permission} is not granted to you",
            ExitCode.REFUSED,
            hint=f"ask {owner.get(scope, 'an org admin')} to review it",
        )
    return permission


def repo_key_of(conn: Connection) -> str:
    return normalise_remote(f"https://{conn.repo.host}/{conn.repo.slug}.git")


# ── status and retirement ────────────────────────────────────────────────────


def records(home: Path) -> list[tuple[Path, dict]]:
    folder = home / SUBMITTED
    found = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            found.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return found


def refresh(home: Path, settings: Mapping[str, Any], warn: Callable[[str], None]) -> list[dict]:
    """Each submission's state; a merged one's personal copy is retired."""
    shown = []
    for path, record in records(home):
        row = {k: record.get(k) for k in ("name", "scope", "team", "number", "url", "state")}
        if record.get("state") == "open":
            ref = RepoRef(**record["repo"])
            with connect_to(ref, settings) as conn, conn.errors():
                pull = review_host(conn).get_pull_request(ref, int(record["number"]))
            if pull.merged:
                record["state"] = "merged"
                record["retired_to"] = retire(home, record, warn)
            elif pull.state != "open":
                record["state"] = "declined"
            if record["state"] != "open":
                record["checked_at"] = _now()
                _write_json(path, record)
        row.update(state=record["state"], retired_to=record.get("retired_to"))
        shown.append(row)
    return shown


def retire(home: Path, record: dict, warn: Callable[[str], None]) -> str | None:
    """Move the personal copy aside (never delete it): the shared copy replaces it."""
    folder = home / "skills" / str(record["name"])
    if not folder.is_dir():
        return None
    try:
        if skill_sha(folder) != record.get("sha256"):
            warn(
                f"{record['name']} changed after it was submitted; "
                "your copy is kept in the retired folder"
            )
    except SkillsError:
        pass
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = home / RETIRED / f"{record['name']}-{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    _move(folder, target)
    return str(target)


def _move(source: Path, target: Path, attempts: int = 5) -> None:
    """`shutil.move`, retried while another program (antivirus, an editor) holds a file."""
    for attempt in range(attempts):
        try:
            shutil.move(str(source), str(target))
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise CliError(
                    f"could not move {source}: another program holds a file in it",
                    ExitCode.ERROR,
                    hint="close editors on the skill and run `agentic skills status` again",
                ) from None
            time.sleep(0.2 * (attempt + 1))


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    temporary.replace(path)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_for_submission(folder: Path, scope: Scope, limits=None) -> list[dict[str, str]]:
    """A personal skill's problems before it is proposed to `scope`: the personal rules
    (it is still instruction-only), and the target scope's own limits."""
    found = check_skill(folder, "personal")
    if scope != "repo":
        found += check_skill(folder, scope, limits=limits)
    return problems_as_dicts(found)


def commit_branch(target: Path, files: dict[str, bytes], sub: Submission) -> tuple[str, str]:
    message = f"{pr_title(sub)}\n\nSubmitted with agentic skills submit (skill {sub.sha256})."
    return repo_setup.commit_on_branch(
        target, dict(files), message, branch=PROMOTION_BRANCH.format(name=sub.name)
    )
