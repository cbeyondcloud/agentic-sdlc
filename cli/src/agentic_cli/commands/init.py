"""`agentic init` (each developer, once per repo) and `agentic init --repo` (the repo
owner, once) - spec §8.1, §24.

Both run the eight repo-root and safety checks first (`agentic_cli.repo_guard`); a
refusal is exit 2 with nothing written.
"""

from pathlib import Path
from typing import Annotated

import typer

from agentic.repoconfig import RepoConfig
from agentic_cli import repo_setup
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
    ) -> None:
        """Set up agentic for this repository (run from the repo root)."""
        out: Output = ctx.obj
        home = agentic_home()
        loaded = resolve_policy(
            standalone=standalone, policy_path=policy, home=home, config=user_config(home)
        )
        try:
            if repo:
                _init_repo(out, loaded, classification, team, pipeline or [])
            else:
                raise CliError("developer init is not available yet", ExitCode.ERROR)
        finally:
            loaded.discard()


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
