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

`review` lists the open submissions of a scope (their blocks unverified: anyone can write
one); `approve` and `decline` first check the pull request itself (`check_files`,
`verify_pull_request`: the files it changes - a rename's old path included - its skill
folder at the head commit against the block's sha256 and the `submit` checks, and its
catalog change), then post a review of that head commit after an RBAC check through the
control plane (`/v1/rbac/check`, the authorizer `init --repo` uses): the scope's
permission is

    repo  agent.approve.repo     (repo maintainers, team leads of the repo's team)
    team  agent.publish.team     (that team's leads)
    org   skills.approve         (org admins)

Without an org there is no RBAC (standalone mode): the command says so and proceeds, as
gate approvals do. In org mode without a reachable control plane, approval is refused.
The check runs on the reviewer's machine, so it is advisory: the host's code owners and
branch protection are what decide the merge.

`status` reads each recorded submission's pull request. A merged skill reaches runs only
once its shared copy is rolled out, so the personal copy is kept (and `status` says so)
until `status --retire` moves it (never deletes it) to
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
from typing import Any, NoReturn

from agentic.hosting import HostingError, RepoRef
from agentic.hosting.base import PullRequest, ReviewHost
from agentic.hosting.git import fetch_branch, push_branch, rev_parse
from agentic.policy import PolicyError, load_policy
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
    unportable,
)
from agentic.skills.catalog import AdmissionLimits
from agentic.yamlio import YamlFileError, load_yaml, split_front_matter
from agentic_cli import control_plane, repo_setup
from agentic_cli.errors import CliError
from agentic_cli.evaluation import SKILL_EVAL_FILE, skill_eval_markdown
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import Connection, connect, connect_to

SUBMITTED = "skills-submitted"
RETIRED = "skills-retired"
EVALS = "skills-eval"
BRANCH_PREFIX = PROMOTION_BRANCH.format(name="")
#: Where the default branch is fetched to before a promotion branch is built on it.
BASE_REF = "refs/agentic/skill-base"
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
    """The skill-eval summary of exactly this skill version that measured: from `given`
    (a folder `skills eval` wrote), else the newest such under
    `~/.agentic/skills-eval/<name>/` - a later run the budget cap stopped does not hide
    an earlier one that measured."""
    candidates = [given] if given is not None else _eval_folders(home / EVALS / name)
    seen_other = False
    unmeasured: tuple[Path, str] | None = None
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
            unmeasured = unmeasured or (folder, str(summary.get("verdict")))
            continue
        summary["folder"] = str(folder)
        return summary
    if unmeasured is not None:
        raise CliError(
            f"no eval of this version of {name} measured (the newest: {unmeasured[1]})",
            ExitCode.REFUSED,
            hint=f"see {unmeasured[0]}; run `agentic skills eval {name}` again",
        )
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


def head_text(repo: Path, path: str, rev: str = "HEAD") -> str | None:
    """`path` as committed at `rev` (LF text), or None when it is not there."""
    done = subprocess.run(
        [*_GIT, "-C", str(repo), "cat-file", "-p", f"{rev}:{path}"], capture_output=True
    )
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def head_has(repo: Path, path: str, rev: str = "HEAD") -> bool:
    done = subprocess.run(
        [*_GIT, "-C", str(repo), "ls-tree", "-z", "--name-only", rev, "--", path],
        capture_output=True,
    )
    return bool(done.stdout.strip(b"\0"))


def branch_name(name: str, sha256: str) -> str:
    """One branch per skill version: a resubmission after an edit is a new branch (and
    pull request), never a force-push over the one a reviewer is reading."""
    return PROMOTION_BRANCH.format(name=f"{name}-{sha256.removeprefix('sha256:')[:8]}")


def org_limits(target: Path):
    """The org's skill admission limits from the policy checkout's `policy/`, or None
    when it does not load there (the policy-repo gate reports why)."""
    try:
        policy = load_policy(target / "policy")
    except PolicyError:
        return None
    return AdmissionLimits(**policy.skill_limits.given())


@dataclass(frozen=True)
class Submission:
    name: str
    scope: Scope
    team: str | None
    sha256: str
    submitter: str
    eval: dict


def build_files(
    target: Path, sub: Submission, source: Path, rev: str = "HEAD"
) -> tuple[dict[str, bytes], list[str]]:
    """Every file of the promotion branch on top of `rev`, and their paths."""
    folder_rel, catalog_rel = promotion_paths(sub.scope, sub.name, sub.team)
    if head_has(target, folder_rel, rev):
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
                head_text(target, catalog_rel, rev), f"{INTERNAL}/{sub.name}", entry
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
    head = "\n".join(
        [
            f"<!-- agentic:skill-submission {json.dumps(block, sort_keys=True)} -->",
            f"## Promote the skill `{sub.name}`",
            "",
            f"Submitted by {sub.submitter} with `agentic skills submit`. The scope owner "
            "reviews it",
            "(`agentic skills review`), and a person merges it through the code owners.",
            "",
            *(f"- `{path}`" for path in paths),
            "",
            "",
        ]
    )
    report = skill_eval_markdown(sub.eval)
    room = MAX_BODY - len(head)
    if len(report) > room:  # only the report is cut: the block stays whole (first line)
        report = report[: max(room - 40, 0)] + "\n\n(report cut: see the eval folder)\n"
    return head + report


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


@dataclass(frozen=True)
class Proposed:
    pull: dict  # number, url, created
    repo: RepoRef
    branch: str
    commit: str | None  # None: this version's pull request was already open
    paths: list[str]


def propose(
    target: Path,
    sub: Submission,
    source: Path,
    settings: Mapping[str, Any],
    warn: Callable[[str], None],
) -> Proposed:
    """Build the branch on the host's default branch - never on whatever the checkout has
    checked out - push it and open its pull request; or find the one this version of the
    skill already has."""
    branch = branch_name(sub.name, sub.sha256)
    with connect(target, settings) as conn, conn.errors():
        provider = conn.provider
        base = provider.default_branch(conn.repo)
        base_rev = _base(target, conn, base, warn)
        files, paths = build_files(target, sub, source, base_rev)
        existing = provider.find_pull_request(conn.repo, branch)
        if existing is not None:
            pull = {"number": existing.number, "url": existing.url, "created": False}
            return Proposed(pull, conn.repo, branch, None, paths)
        message = f"{pr_title(sub)}\n\nSubmitted with agentic skills submit (skill {sub.sha256})."
        name, commit = repo_setup.commit_on_branch(
            target, dict(files), message, branch=branch, base=base_rev
        )
        push_branch(target, conn.remote, name, secrets=[conn.token])
        opened = provider.open_pull_request(
            conn.repo, head=name, base=base, title=pr_title(sub), body=pr_body(sub, paths)
        )
    pull = {"number": opened.number, "url": opened.url, "created": True}
    return Proposed(pull, conn.repo, name, commit, paths)


def _base(target: Path, conn: Connection, base: str, warn: Callable[[str], None]) -> str:
    """The commit of `base` on the remote: fetched now; when the fetch fails, the
    remote-tracking branch from the last fetch, said so."""
    try:
        return fetch_branch(target, conn.remote, base, BASE_REF, secrets=[conn.token])
    except HostingError as error:
        tracking = f"refs/remotes/{conn.remote}/{base}"
        try:
            found = rev_parse(target, tracking)
        except HostingError:
            raise CliError(
                f"could not read {base} from {conn.remote}: {error}", ExitCode.ERROR
            ) from None
        warn(f"could not fetch {base} ({error}); building on {conn.remote}/{base} as last fetched")
        return found


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
    path = folder / record_name(sub.name, sub.scope, sub.team)
    _write_json(path, record)
    return path


def record_name(name: str, scope: str, team: str | None) -> str:
    """One record per skill and scope (and team), so submitting one skill to two scopes
    keeps both."""
    return f"{name}.{scope}{f'.{team}' if team else ''}.json"


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
                # the block is the submitter's word; `approve` checks the files against it
                "verified": False,
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


def refresh(
    home: Path,
    settings: Mapping[str, Any],
    warn: Callable[[str], None],
    *,
    retire_merged: bool = False,
) -> list[dict]:
    """Each submission's state. A merged one's personal copy is kept (review F2): the
    shared copy reaches runs only once it is rolled out (team and repo skills are not
    loaded at run time yet), so the copy is retired only when the person asks
    (`retire_merged`, `agentic skills status --retire`)."""
    shown = []
    for path, record in records(home):
        row = {k: record.get(k) for k in ("name", "scope", "team", "number", "url", "state")}
        changed = False
        if record.get("state") == "open":
            ref = RepoRef(**record["repo"])
            with connect_to(ref, settings) as conn, conn.errors():
                pull = review_host(conn).get_pull_request(ref, int(record["number"]))
            if pull.merged:
                record["state"] = "merged"
            elif pull.state != "open":
                record["state"] = "declined"
            if record["state"] != "open":
                record["checked_at"] = _now()
                changed = True
        kept = None
        if record["state"] == "merged" and not record.get("retired_to"):
            folder = home / "skills" / str(record["name"])
            if retire_merged:
                record["retired_to"] = retire(home, record, warn)
                changed = True
            elif folder.is_dir():
                kept = str(folder)
                warn(
                    f"{record['name']} was merged; your personal copy {folder} is kept: the "
                    "shared copy reaches runs only once it is rolled out. When it has, run "
                    f"`agentic skills status --retire` (the copy is moved to "
                    f"{home / RETIRED}, never deleted)"
                )
        if changed:
            _write_json(path, record)
        row.update(state=record["state"], retired_to=record.get("retired_to"), kept=kept)
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


def check_for_submission(
    folder: Path, scope: Scope, limits: AdmissionLimits | None = None
) -> list[dict[str, str]]:
    """A personal skill's problems before it is proposed to `scope`: the personal rules
    (it is still instruction-only) and, for the policy repo, what its gate will check -
    the org's size limits, and no declared licence but `internal` (the catalog entry is an
    org-written skill's, and admission refuses a SKILL.md that disagrees with it)."""
    found = check_skill(folder, "personal")
    if scope != "repo":
        found += check_skill(folder, scope, limits=limits)
        declared = _front_matter(folder).get("license")
        if declared not in (None, INTERNAL):
            why = (
                f"it declares license: {declared}; a skill promoted to the {scope} catalog is "
                f"`{INTERNAL}` - remove the license line"
            )
            found.append(Rejection("licence", "SKILL.md", why))
    return problems_as_dicts(found)


def _front_matter(folder: Path) -> dict:
    try:
        header, _ = split_front_matter((folder / "SKILL.md").read_text(encoding="utf-8-sig"))
        data = load_yaml(header, first_line=2)[0]
    except (OSError, UnicodeDecodeError, YamlFileError):
        return {}
    return data if isinstance(data, dict) else {}


def scope_prefix(scope: str, team: str | None) -> str:
    """Where a submission to `scope` may change files, in its target repository."""
    if scope == "repo":
        return ".agentic/skills/"
    if scope == "team":
        return f"teams/{team}/skills/"
    return "skills/"


def check_files(scope: str, team: str | None, files: list[str]) -> None:
    """Refuse (exit 2) a submission that changes anything outside its scope's folder: the
    RBAC check is bound to the files a pull request changes (a renamed file's old path
    included), never to the team its (submitter-written) description names."""
    prefix = scope_prefix(scope, team)
    outside = [path for path in files if not path.startswith(prefix)]
    if not files or outside:
        shown = ", ".join(outside[:5]) or "no files"
        raise CliError(
            f"this {scope} submission must change only {prefix}, but it changes {shown}",
            ExitCode.REFUSED,
            hint="review it as a normal pull request instead",
        )


def verify_pull_request(
    host: ReviewHost,
    repo: RepoRef,
    pull: PullRequest,
    block: dict,
    files: list[str],
    *,
    scope: Scope,
    team: str | None,
    limits: AdmissionLimits | None = None,
) -> None:
    """Refuse (exit 2) a submission whose pull request does not hold what its block says
    (review F3) - the block is the submitter's text, and anyone can write one:

      * it changes only the skill's own folder and, for team or org, its catalog;
      * the folder at the head commit hashes to the block's sha256 (the version the
        eval report names) and passes the checks `submit` runs;
      * the catalog gains exactly the entry `submit` writes for it, and nothing else
        changes there against the commit the pull request is compared with.

    The eval verdict in the block stays the submitter's word: the report itself is not
    on the host. The host's code owners and branch protection still decide the merge."""
    name, sha = block.get("name"), block.get("sha256")
    if not isinstance(name, str) or not isinstance(sha, str):
        _refuse(pull, "its submission block names no skill and version")
    try:
        folder_rel, catalog_rel = promotion_paths(scope, name, team)
    except SkillsError as error:
        _refuse(pull, str(error))
    inside = [path for path in files if path.startswith(f"{folder_rel}/")]
    outside = [path for path in files if path not in inside and path != catalog_rel]
    if outside or not inside:
        wanted = f"{folder_rel}/" + (f" and {catalog_rel}" if catalog_rel else "")
        _refuse(pull, f"it must change only {wanted}, but it changes {outside[:5] or 'neither'}")
    with tempfile.TemporaryDirectory(prefix="agentic-review-") as scratch:
        folder = Path(scratch) / name
        for path in inside:
            relative = path[len(folder_rel) + 1 :]
            parts = relative.split("/")
            if any(part in ("", ".", "..") for part in parts):
                _refuse(pull, f"{path} is not a plain path inside the skill")
            if why := unportable(relative):
                _refuse(pull, f"{path}: {why}")
            data = host.file_at(repo, path, pull.head_sha)
            if data is None:
                _refuse(pull, f"{path} is removed or is not a file at its head commit")
            target = folder.joinpath(*parts)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError as error:
                _refuse(pull, f"{path} cannot be written on this machine ({error})")
        if skill_sha(folder) != sha:
            _refuse(pull, f"its files are not the version its description names ({sha})")
        problems = check_for_submission(folder, scope, limits)
        if problems:
            first = problems[0]
            _refuse(pull, f"{folder_rel}/{first['path']}: {first['reason']}")
    if catalog_rel is not None:
        _verify_catalog(host, repo, pull, catalog_rel, name, sha, folder_rel)


def _verify_catalog(
    host: ReviewHost,
    repo: RepoRef,
    pull: PullRequest,
    catalog_rel: str,
    name: str,
    sha: str,
    folder_rel: str,
) -> None:
    key = f"{INTERNAL}/{name}"
    head = _catalog_document(pull, catalog_rel, host.file_at(repo, catalog_rel, pull.head_sha))
    ancestor = host.common_ancestor(repo, pull.base, pull.head_sha)
    base = _catalog_document(pull, catalog_rel, host.file_at(repo, catalog_rel, ancestor))
    skills = dict(head.get("skills") or {})
    entry = skills.pop(key, None)
    owner = entry.get("owner") if isinstance(entry, dict) else None
    if not isinstance(owner, str) or entry != catalog_entry(
        name, sha, owner=owner, path=folder_rel
    ):
        _refuse(pull, f"{catalog_rel} does not hold the entry submit writes for {key} ({sha})")
    rest = {**head, "skills": skills}
    for document in (rest, base):
        if not document.get("skills"):
            document.pop("skills", None)
    if rest != base:
        _refuse(pull, f"it changes {catalog_rel} beyond the entry of {key}")


def _catalog_document(pull: PullRequest, catalog_rel: str, data: bytes | None) -> dict:
    """A catalog file's YAML as plain data ({} when the file is not there)."""
    if data is None:
        return {}
    try:
        document = load_yaml(data.decode("utf-8-sig"))[0]
    except (UnicodeDecodeError, YamlFileError) as error:
        _refuse(pull, f"{catalog_rel} cannot be read: {error}")
    if document is None:
        return {}
    if not isinstance(document, dict) or not isinstance(document.get("skills") or {}, dict):
        _refuse(pull, f"{catalog_rel} is not a catalog")
    return dict(document)


def _refuse(pull: PullRequest, why: str) -> NoReturn:
    raise CliError(
        f"#{pull.number} is not a skill submission as `agentic skills submit` makes one: {why}",
        ExitCode.REFUSED,
        hint="review it as a normal pull request, or ask the submitter to submit it again",
    )
