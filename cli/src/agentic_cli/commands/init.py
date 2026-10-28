"""`agentic init` (each developer, once per repo) and `agentic init --repo` (the repo
owner, once) - spec §8.1, §24.

Both run the eight repo-root and safety checks first (`agentic_cli.repo_guard`); a
refusal is exit 2 with nothing written.

Org mode with a control plane (SP15): the person's roles come from the control plane
(`/v1/me`) and the user config's `roles` is ignored, with a warning (DF-203); check 7 of
`init --repo` asks the control plane whether the person holds `agent.edit.repo` for the
team and repo (DF-184), and the repo is registered with the control plane afterwards. In
standalone mode `roles` still chooses which authoring skills are installed - it never
authorises anything.
"""

import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from rich.markup import escape

from agentic.hosting.base import scrub_url_credentials
from agentic.hosting.git import push_branch
from agentic.orgapi import Me
from agentic.policy.repos import normalise_remote
from agentic.repoconfig import RepoConfig
from agentic_cli import control_plane, dev_init, repo_setup
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import connect
from agentic_cli.output import Output
from agentic_cli.policy_source import LoadedPolicy, resolve_policy
from agentic_cli.repo_guard import check_repo, origin_url
from agentic_cli.workspace import recorded_policy, recorded_root


def register(app: typer.Typer) -> None:
    @app.command("init")
    def init(
        ctx: typer.Context,
        repo: bool = typer.Option(
            False, "--repo", help="Set up the repository itself: shared files on a new branch."
        ),
        standalone: bool = typer.Option(
            False, "--standalone", help="Use a local policy: no control plane, no RBAC."
        ),
        policy: Annotated[
            Path | None,
            typer.Option("--policy", help="With --standalone: the policy repo or policy folder."),
        ] = None,
        classification: str | None = typer.Option(
            None, "--classification", help="--repo: the repo's class (default: policy's)."
        ),
        team: str | None = typer.Option(None, "--team", help="--repo: the owning team."),
        pipeline: Annotated[
            list[str] | None,
            typer.Option("--pipeline", help="--repo: a pipeline enabled for the repo (repeat)."),
        ] = None,
        install_missing: bool = typer.Option(
            False,
            "--install-missing",
            help="Also install the org's on-request user-space tools (shows them, then asks).",
        ),
        yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before installing."),
        refresh_template: bool = typer.Option(
            False,
            "--refresh-template",
            help="With --standalone: replace your copy of the framework's template with "
            "this version's, keeping your policy/ files (the old copy is saved beside it).",
        ),
        open_pr: bool = typer.Option(
            True,
            "--pr/--no-pr",
            help="--repo: push the branch and open its pull request (default), or only "
            "prepare the branch locally.",
        ),
    ) -> None:
        """Set up agentic for this repository (run from the repo root)."""
        out: Output = ctx.obj
        home = agentic_home()
        config = user_config(home)
        if install_missing and out.json_mode and not yes:
            raise CliError(
                "--install-missing asks before installing, which --json cannot",
                ExitCode.USAGE,
                hint="add --yes after reviewing the tools with a plain `agentic init`",
            )
        if refresh_template and repo:
            raise CliError(
                "--refresh-template refreshes your own standalone copy, not the repository",
                ExitCode.USAGE,
                hint="run `agentic init --standalone --refresh-template` without --repo",
            )
        recorded = None
        if standalone and policy is None and (found := recorded_policy(Path.cwd())):
            recorded = recorded_root(found, Path.cwd())  # what init chose last time
        loaded = resolve_policy(
            standalone=standalone,
            policy_path=policy,
            home=home,
            config=config,
            recorded=recorded,
            refresh_template=refresh_template,
        )
        try:
            if repo:
                settings = config if open_pr else None
                client = _client(loaded, config, home)
                _init_repo(out, loaded, classification, team, pipeline or [], settings, client)
            else:
                _init_developer(out, loaded, home, config, install_missing, yes)
        finally:
            loaded.discard()


def _init_developer(
    out: Output, loaded: LoadedPolicy, home: Path, config: dict, install: bool, yes: bool
) -> None:
    root = check_repo(
        Path.cwd(),
        mode="developer",
        home=Path.home(),
        policy=loaded.policy,
        org_mode=loaded.mode == "org",
    )
    deps = dev_init.default_deps()
    roles = config.get("roles") or []
    configured = tuple(str(role) for role in roles) if isinstance(roles, list) else ()
    if loaded.mode == "org" and control_plane.control_plane_url(os.environ, config):
        # org roles come from the control plane, never from the user config (DF-203)
        deps.roles = _org_roles(loaded.me, _team(None, config)) if loaded.me else ()
        if configured:
            out.warn(
                "roles in ~/.agentic/config.yaml are ignored: in org mode your roles come "
                "from the control plane"
            )
    else:  # standalone: chooses authoring skills only, never authorises anything
        deps.roles = configured
    console = out.err if out.json_mode else out.out

    def announce(plan: list[dict]) -> None:
        console.print("These tools will be installed (user space only):")
        for tool in plan:
            console.print(f"  {tool['tool']} {tool['version']}", markup=False)
            console.print(f"    from {tool['url']}", markup=False)
            console.print(f"    to   {tool['to']}", markup=False)

    report = dev_init.run_dev_init(
        root, loaded, home, deps, install_missing=install, assume_yes=yes, announce=announce
    )
    out.result(report.to_json(), report.show)
    if not report.ready:
        missing = [item.name for item in report.items if item.status == "missing"]
        raise CliError(
            f"init finished, but agents cannot run until this is fixed: {', '.join(missing)}",
            ExitCode.ERROR,
            hint="each item marked missing above says how to fix it; then run `agentic init` again",
        )


def _init_repo(
    out: Output,
    loaded: LoadedPolicy,
    classification: str | None,
    team: str | None,
    pipelines: list[str],
    settings: dict | None,
    client: control_plane.ControlPlaneClient | None = None,
) -> None:
    """`settings` (the user config) opens the pull request; None (`--no-pr`) does not.
    `client` (org mode with a control plane) answers check 7 and registers the repo."""
    remote = origin_url(Path.cwd())
    owning_team = _team(team, settings or {})
    permitted = None
    if client is not None:
        key = normalise_remote(remote) if remote else None

        def permitted(permission: str) -> bool:
            return client.check(permission, team=owning_team, repo=key).allowed

    root = check_repo(
        Path.cwd(),
        mode="repo",
        home=Path.home(),
        policy=loaded.policy,
        org_mode=loaded.mode == "org",
        permitted=permitted,
    )
    chosen = classification or loaded.policy.default_classification
    if chosen not in loaded.policy.classifications:
        known = ", ".join(sorted(loaded.policy.classifications))
        raise CliError(
            f"classification '{chosen}' is not defined in org policy",
            ExitCode.INVALID,
            hint=f"choose one of: {known}",
        )
    try:
        config = RepoConfig(classification=chosen, team=team, pipelines=list(pipelines))
    except ValueError as error:
        raise CliError(str(error).splitlines()[0], ExitCode.INVALID) from error
    files = repo_setup.shared_files(root.path, loaded.policy, config)
    curated = repo_setup.existing_agents_md(root.path)
    kept = [curated] if curated else []
    branch, commit = repo_setup.commit_on_branch(
        root.path, files, "chore: set up agentic for this repository (agentic init --repo)"
    )
    default = root.default_branch or "main"
    if root.empty:
        steps = repo_setup.first_commit_steps(default, branch)
    else:
        steps = repo_setup.next_steps(
            root.origin, default, branch, local=loaded.mode == "standalone"
        )
    data = {"branch": branch, "commit": commit, "files": sorted(files), "kept": kept}
    data["next"] = steps
    if client is not None:
        data["registered"] = _register(out, client, remote, owning_team)
    failure: CliError | None = None
    if settings is None:
        data["pull_request"] = {"opened": False, "why": "--no-pr"}
    elif root.empty:
        why = (
            "the repository has no commits yet, so there is no branch to open a pull "
            f"request against: make {branch} its first commit"
        )
        data["pull_request"] = {"opened": False, "why": why}
    elif root.origin is None:  # nothing to push to: the local merge is the way
        data["pull_request"] = {"opened": False, "why": "no origin remote"}
    else:
        try:
            data["pull_request"] = _open_pull_request(root.path, branch, data, settings)
        except _NotOpened as skipped:  # no known host or no token: the manual steps
            data["pull_request"] = {"opened": False, "why": skipped.why}
            out.warn(f"the pull request was not opened: {skipped.why}")
        except CliError as error:  # the push or the API failed: an error, after the result
            data["pull_request"] = {"opened": False, "why": str(error)}
            manual = f"push and open it by hand: {steps['push']}, then {steps['pull_request']}"
            hint = f"{error.hint}; or {manual}" if error.hint else manual
            failure = CliError(str(error), ExitCode.ERROR, hint=hint)
    pull = data["pull_request"]

    def show(console) -> None:
        console.print(
            f"Prepared branch [bold]{escape(branch)}[/bold] ({commit[:12]}) with:", markup=True
        )
        for path in sorted(files):
            console.print(f"  {path}", markup=False)
        for path in kept:
            console.print(f"Kept your {path} as it is (no draft proposed).", markup=False)
        console.print("Your checkout was not changed.")
        if pull["opened"]:
            verb = "Opened" if pull["created"] else "Updated"
            console.print(f"{verb} the pull request: {pull['url']}", markup=False)
            return
        if root.empty:
            console.print(f"No pull request: {pull['why']}.", markup=False)
            console.print("Next:")
            for step in steps.values():
                console.print(f"  {step}", markup=False)
            return
        console.print("Next, to share it with your team:")
        console.print(f"  {steps['push']}", markup=False)
        console.print(f"  {steps['pull_request']}", markup=False)
        if "merge" in steps:
            console.print("Or, to use it in this clone only:")
            console.print(f"  {steps['merge']}", markup=False)

    out.result(data, show)
    if failure is not None:
        raise failure


def _client(
    loaded: LoadedPolicy, config: dict, home: Path
) -> control_plane.ControlPlaneClient | None:
    """The control-plane client in org mode, or None (standalone, or no control plane)."""
    if loaded.mode != "org":
        return None
    return control_plane.client_for(os.environ, config, home)


def _team(given: str | None, config: dict) -> str | None:
    return given or os.environ.get("AGENTIC_TEAM", "").strip() or config.get("team") or None


def _org_roles(me: Me, team: str | None) -> tuple[str, ...]:
    """The roles `me` holds at org scope or in `team` (they choose the authoring skills)."""
    held = {a.role for a in me.assignments if a.scope == "org" or (team and a.team == team)}
    return tuple(sorted(held))


def _register(
    out: Output, client: control_plane.ControlPlaneClient, remote: str | None, team: str | None
) -> str | None:
    """Register the repo with the control plane (idempotent); a failure is a warning."""
    if not remote or not team:
        out.warn("the repo was not registered with the control plane: no origin or no team")
        return None
    try:
        return client.register_repo(_shareable(remote), team).repo_key
    except control_plane.ControlPlaneError as error:
        out.warn(f"the repo was not registered with the control plane: {error}")
        return None


def _shareable(remote: str) -> str:
    """The remote as the control plane may store it: an https URL loses any credentials;
    an ssh user (`git@`) is part of the address and is kept (review F3)."""
    parsed = urlsplit(remote)
    if parsed.password or (parsed.scheme in ("http", "https") and parsed.username):
        return scrub_url_credentials(remote)
    return remote


class _NotOpened(Exception):
    """The pull request cannot be opened from here (no known host, no token)."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _open_pull_request(repo: Path, branch: str, data: dict, settings: dict) -> dict:
    """Push `branch` and open (or find) its pull request through the hosting adapter."""
    try:
        conn = connect(repo, settings)
    except CliError as error:
        raise _NotOpened(f"{error}" + (f" ({error.hint})" if error.hint else "")) from error
    with conn, conn.errors():
        push_branch(repo, conn.remote, branch, secrets=[conn.token])
        provider = conn.provider
        existing = provider.find_pull_request(conn.repo, branch)
        if existing is not None:
            return {
                "opened": True,
                "created": False,
                "number": existing.number,
                "url": existing.url,
            }
        body = repo_setup.pr_body(data["files"], data["kept"])
        pull = provider.open_pull_request(
            conn.repo,
            head=branch,
            base=provider.default_branch(conn.repo),
            title=repo_setup.PR_TITLE,
            body=body,
        )
    return {"opened": True, "created": True, "number": pull.number, "url": pull.url}
