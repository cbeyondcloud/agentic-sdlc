"""`agentic init` (each developer, once per repo) and `agentic init --repo` (the repo
owner, once) - spec §8.1, §24.

Both run the eight repo-root and safety checks first (`agentic_cli.repo_guard`); a
refusal is exit 2 with nothing written.
"""

from pathlib import Path
from typing import Annotated

import typer

from agentic.repoconfig import RepoConfig
from agentic_cli import dev_init, repo_setup
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.policy_source import LoadedPolicy, resolve_policy
from agentic_cli.repo_guard import check_repo


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
        loaded = resolve_policy(standalone=standalone, policy_path=policy, home=home, config=config)
        try:
            if repo:
                _init_repo(out, loaded, classification, team, pipeline or [])
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
    deps.roles = tuple(str(role) for role in roles) if isinstance(roles, list) else ()
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
        raise CliError(
            "init finished, but this machine is not ready",
            ExitCode.ERROR,
            hint="fix the items marked missing above, then run `agentic init` again",
        )


def _init_repo(
    out: Output,
    loaded: LoadedPolicy,
    classification: str | None,
    team: str | None,
    pipelines: list[str],
) -> None:
    root = check_repo(
        Path.cwd(),
        mode="repo",
        home=Path.home(),
        policy=loaded.policy,
        org_mode=loaded.mode == "org",
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
    branch, commit = repo_setup.commit_on_branch(
        root.path, files, "chore: set up agentic for this repository (agentic init --repo)"
    )
    steps = repo_setup.next_steps(root.origin, root.default_branch or "main", branch)
    data = {"branch": branch, "commit": commit, "files": sorted(files), "next": steps}

    def show(console) -> None:
        console.print(f"Prepared branch [bold]{branch}[/bold] ({commit[:12]}) with:")
        for path in sorted(files):
            console.print(f"  {path}", markup=False)
        console.print("Your checkout was not changed. Next:")
        console.print(f"  {steps['push']}", markup=False)
        console.print(f"  {steps['pull_request']}", markup=False)

    out.result(data, show)
