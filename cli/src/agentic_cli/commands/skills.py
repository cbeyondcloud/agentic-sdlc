"""Authoring and promoting skills (M2-46; spec §24 "Custom skills", D30):

agentic skills new NAME [--description TEXT]      a personal skill from the template
agentic skills validate [NAME|PATH...] [--scope]  format, size, secrets, scripts, widening
agentic skills eval NAME --agent A [...]          the agent with and without the skill
agentic skills submit NAME --to repo|team|org     a pull request with the eval report
agentic skills review [--to S] [--team T]         the open submissions of a scope
agentic skills approve N --to S                   the scope owner approves (RBAC; the
                                                  pull request's files are checked first)
agentic skills decline N --to S --reason R        ... or asks for changes
agentic skills status [--retire]                  your submissions; --retire moves the
                                                  personal copy of a merged one aside

The commands join the `skills` group `catalog.py` creates (list, search).
"""

import os
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Annotated

import typer

from agentic.hosting.base import ListingIncomplete
from agentic.skills import SkillsError
from agentic.skills.authoring import SCOPES, check_skill, scaffold
from agentic.skills.personal import load_personal, skill_block
from agentic_cli import evaluation
from agentic_cli import skill_promotion as promo
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import connect
from agentic_cli.output import Output
from agentic_cli.workspace import find_repo

ScopeOption = Annotated[str, typer.Option("--to", help="repo, team or org.")]
TeamOption = Annotated[
    str | None, typer.Option("--team", help="The team (default: your team in the config).")
]
CheckoutOption = Annotated[
    Path | None,
    typer.Option(
        "--policy-checkout",
        help="A clone of the policy repo (team and org; default: policy_checkout, else a "
        "fresh clone of policy_repo).",
    ),
]


def register(app: typer.Typer) -> None:
    group = next((i.typer_instance for i in app.registered_groups if i.name == "skills"), None)
    if group is None:  # catalog.py registers the group first; standalone use still works
        group = typer.Typer(help="Skills: author, evaluate, submit, review.", no_args_is_help=True)
        app.add_typer(group, name="skills")

    @group.command("new")
    def new(
        ctx: typer.Context,
        name: Annotated[str, typer.Argument(help="lowercase-with-hyphens")],
        description: Annotated[
            str | None, typer.Option(help="When an agent should use it (one sentence).")
        ] = None,
    ) -> None:
        """Create a personal skill, ~/.agentic/skills/NAME/SKILL.md, from the template."""
        out: Output = ctx.obj
        try:
            folder = scaffold(agentic_home(), name, description)
        except SkillsError as error:
            raise CliError(str(error), ExitCode.INVALID) from error
        path = folder / "SKILL.md"
        out.result(
            {"name": name, "path": str(path)},
            lambda c: c.print(
                f"created {path}\nedit it, then: agentic skills validate {name}", markup=False
            ),
        )

    @group.command("validate")
    def validate(
        ctx: typer.Context,
        targets: Annotated[
            list[str] | None,
            typer.Argument(help="Personal skill names or skill folders (default: all yours)."),
        ] = None,
        scope: Annotated[
            str, typer.Option(help="Check as a personal, repo, team or org skill.")
        ] = "personal",
    ) -> None:
        """Check skills before use or promotion (exit 3 when any has a problem)."""
        out: Output = ctx.obj
        _scope(scope, allowed=SCOPES)
        home = agentic_home()
        folders = [_target(home, t) for t in targets] if targets else _all_personal(home)
        rows = []
        for folder in folders:
            problems = promo.problems_as_dicts(check_skill(folder, scope))  # type: ignore[arg-type]
            rows.append({"name": folder.name, "path": str(folder), "problems": problems})
        data = {"scope": scope, "skills": rows}
        out.result(data, lambda c: _show_validation(c, rows))
        bad = [row["name"] for row in rows if row["problems"]]
        if bad:
            raise CliError(f"{len(bad)} skill(s) with problems: {', '.join(bad)}", ExitCode.INVALID)

    @group.command("eval")
    def eval_skill(
        ctx: typer.Context,
        name: Annotated[str, typer.Argument(help="Your personal skill.")],
        agent: Annotated[str, typer.Option("--agent", help="The agent definition's name.")],
        corpus: Annotated[Path | None, typer.Option(help="The eval corpus folder.")] = None,
        split: Annotated[str, typer.Option(help="holdout, dev or all.")] = "holdout",
        repeat: Annotated[int | None, typer.Option(min=1, help="Runs per case and side.")] = None,
        mode: Annotated[str, typer.Option(help="live, record or replay.")] = "live",
        budget_usd: Annotated[
            float | None, typer.Option("--budget-usd", min=0.01, help="Spend cap.")
        ] = None,
        policy: Annotated[Path | None, typer.Option(help="The org policy folder.")] = None,
        agents: Annotated[Path | None, typer.Option(help="Agent definitions folder.")] = None,
        cassettes: Annotated[Path | None, typer.Option(help="Recorded responses.")] = None,
        gateway: Annotated[str | None, typer.Option(help="TokenLean's URL.")] = None,
        seed: Annotated[int, typer.Option(help="Seed for run order and labels.")] = 0,
        tag: Annotated[str | None, typer.Option(help="Only cases carrying this tag.")] = None,
        classification: Annotated[
            str | None, typer.Option(help="Data class of cases that name none.")
        ] = None,
        out_dir: Annotated[Path | None, typer.Option("--out", help="Where to write.")] = None,
    ) -> None:
        """Run AGENT over corpus cases with and without the skill (blind) and compare."""
        output: Output = ctx.obj
        home = agentic_home()
        folder = promo.personal_folder(home, name)
        problems = promo.problems_as_dicts(check_skill(folder, "personal"))
        if problems:
            raise CliError(
                f"{name} has problems; fix them before evaluating it: {problems[0]['reason']}",
                ExitCode.INVALID,
                hint=f"agentic skills validate {name}",
            )
        loaded = load_personal(home)
        skill = next((s for s in loaded.skills if s.name == name), None)
        if skill is None:
            why = next((r.reason for r in loaded.rejected if r.path.endswith(f"/{name}")), "")
            raise CliError(f"{name} cannot be loaded: {why}", ExitCode.INVALID)
        variant = evaluation.SkillVariant(skill.name, skill.sha256, skill_block([skill]))
        stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{token_hex(3)}"
        target = out_dir or home / promo.EVALS / name / stamp
        result = evaluation.execute_run(
            evaluation.RunOptions(
                agent=agent,
                corpus=corpus,
                split=split,
                repeat=repeat,
                mode=mode,
                budget_usd=budget_usd,
                out=target,
                policy=policy,
                agents=agents,
                cassettes=cassettes,
                gateway=gateway,
                seed=seed,
                classification=classification,
                tag=tag,
                skill=variant,
            )
        )
        summary = evaluation.write_skill_eval(result, variant)
        data = {**summary, "folder": str(result.folder), "report": str(result.paths["json"])}
        output.result(
            data, lambda c: c.print(evaluation.skill_eval_markdown(summary), markup=False)
        )
        if (error := evaluation.failure(result)) is not None:
            raise error

    @group.command("submit")
    def submit(
        ctx: typer.Context,
        name: Annotated[str, typer.Argument(help="Your personal skill.")],
        to: ScopeOption,
        team: TeamOption = None,
        report: Annotated[
            Path | None, typer.Option(help="The folder `skills eval` wrote (default: newest).")
        ] = None,
        checkout: CheckoutOption = None,
    ) -> None:
        """Propose the skill to the owner of a scope: a pull request with its eval report."""
        out: Output = ctx.obj
        scope = _scope(to)
        home = agentic_home()
        settings = user_config(home)
        folder = promo.personal_folder(home, name)
        chosen_team = _team(team, settings) if scope == "team" else None
        with _target_repo(scope, settings, checkout) as target:
            limits = promo.org_limits(target) if scope != "repo" else None
            problems = promo.check_for_submission(folder, scope, limits)  # type: ignore[arg-type]
            if problems:
                first = problems[0]
                raise CliError(
                    f"{name} cannot be submitted: {first['path']}: {first['reason']}",
                    ExitCode.INVALID,
                    hint=f"agentic skills validate {name} --scope {scope}",
                )
            sha = promo.skill_sha(folder)
            summary = promo.latest_eval(home, name, sha, report)
            person = _person(settings, target)
            sub = promo.Submission(name, scope, chosen_team, sha, person, summary)  # type: ignore[arg-type]
            done = promo.propose(target, sub, folder, settings, out.warn)
            record = promo.record_submission(home, sub, done.repo, done.pull, done.branch)
        pull = done.pull
        data = {
            "name": name,
            "scope": scope,
            "team": chosen_team,
            "sha256": sha,
            "branch": done.branch,
            "commit": done.commit,
            "files": done.paths,
            "eval": summary["folder"],
            "pull_request": pull,
            "record": str(record),
        }
        out.result(
            data,
            lambda c: c.print(
                f"{name} submitted to {scope}: {pull['url']}\n"
                "the scope owner reviews it; `agentic skills status` shows when it is merged",
                markup=False,
            ),
        )

    @group.command("review")
    def review(
        ctx: typer.Context,
        to: Annotated[str | None, typer.Option("--to", help="repo, team or org.")] = None,
        team: TeamOption = None,
        checkout: CheckoutOption = None,
    ) -> None:
        """List the open skill submissions of a scope (default: this repo's)."""
        out: Output = ctx.obj
        scope = _scope(to or "repo")
        settings = user_config(agentic_home())
        with _target_repo(scope, settings, checkout) as target:
            with connect(target, settings) as conn:
                rows = promo.pending(conn, scope, team if scope == "team" else None)
        out.result({"scope": scope, "submissions": rows}, lambda c: _show_pending(c, rows))

    @group.command("approve")
    def approve(
        ctx: typer.Context,
        number: Annotated[int, typer.Argument(help="The submission's pull request number.")],
        to: ScopeOption,
        checkout: CheckoutOption = None,
    ) -> None:
        """Approve a skill submission as the owner of its scope (RBAC-checked)."""
        _decide(ctx.obj, number, to, checkout, approve=True, reason="")

    @group.command("decline")
    def decline(
        ctx: typer.Context,
        number: Annotated[int, typer.Argument(help="The submission's pull request number.")],
        to: ScopeOption,
        reason: Annotated[str, typer.Option(help="What would make it acceptable.")],
        checkout: CheckoutOption = None,
    ) -> None:
        """Ask for changes to a skill submission, saying why (RBAC-checked)."""
        _decide(ctx.obj, number, to, checkout, approve=False, reason=reason)

    @group.command("status")
    def status(
        ctx: typer.Context,
        retire: Annotated[
            bool,
            typer.Option(
                "--retire",
                help="Move the personal copy of each merged skill to ~/.agentic/skills-retired/"
                " (once its shared copy reaches your runs).",
            ),
        ] = False,
    ) -> None:
        """Your submissions; a merged one's personal copy is kept until --retire."""
        out: Output = ctx.obj
        home = agentic_home()
        rows = promo.refresh(home, user_config(home), out.warn, retire_merged=retire)
        out.result({"submissions": rows}, lambda c: _show_status(c, rows))


# ── helpers ──────────────────────────────────────────────────────────────────


def _decide(
    out: Output, number: int, to: str, checkout: Path | None, *, approve: bool, reason: str
) -> None:
    scope = _scope(to)
    home = agentic_home()
    settings = user_config(home)
    with _target_repo(scope, settings, checkout) as target:
        with connect(target, settings) as conn:
            host = promo.review_host(conn)
            with conn.errors():
                pull = host.get_pull_request(conn.repo, number)
            block = promo.submission_of(pull.body)
            if block is None or pull.state != "open":
                raise CliError(f"#{number} is not an open skill submission", ExitCode.INVALID)
            if block.get("scope") != scope:
                raise CliError(
                    f"#{number} is a {block.get('scope')} submission, not {scope}",
                    ExitCode.INVALID,
                )
            team = block.get("team") if scope == "team" else None
            with conn.errors():
                try:
                    files = host.pull_request_files(conn.repo, number)
                except ListingIncomplete as error:
                    raise CliError(
                        f"#{number}: {error}; the files not listed cannot be checked",
                        ExitCode.REFUSED,
                        hint="review it as a normal pull request instead",
                    ) from error
            promo.check_files(scope, team, files)  # the team checked is the files' team
            with conn.errors():
                if approve:  # a decline needs no proof: asking for changes is always safe
                    limits = promo.org_limits(target) if scope != "repo" else None
                    promo.verify_pull_request(
                        host, conn.repo, pull, block, files, scope=scope, team=team, limits=limits
                    )
                if host.get_pull_request(conn.repo, number).head_sha != pull.head_sha:
                    raise CliError(
                        f"#{number} changed while it was being checked",
                        ExitCode.REFUSED,
                        hint="run the command again",
                    )
            repo_key = promo.repo_key_of(conn) if scope == "repo" else None
            permission = promo.authorize(
                scope, team, repo_key, settings, os.environ, home, out.warn
            )
            verb = "approved" if approve else "declined"
            body = f"Skill submission {verb} with `agentic skills` ({permission})."
            if reason:
                body += f"\n\n{reason}"
            with conn.errors():
                host.review_pull_request(
                    conn.repo, number, approve=approve, body=body, commit=pull.head_sha or None
                )
    data = {"number": number, "skill": block.get("name"), "scope": scope, "decision": verb}
    out.result(data, lambda c: c.print(f"#{number} {block.get('name')}: {verb}", markup=False))


def _target_repo(scope: str, settings: dict, checkout: Path | None):
    if scope == "repo":
        return nullcontext(find_repo(Path.cwd()))
    return promo.policy_checkout(settings, checkout)


def _scope(value: str, allowed: tuple[str, ...] = ("repo", "team", "org")) -> str:
    if value not in allowed:
        raise CliError(f"the scope is one of {', '.join(allowed)}, not {value!r}", ExitCode.USAGE)
    return value


def _team(given: str | None, settings: dict) -> str:
    team = given or os.environ.get("AGENTIC_TEAM", "").strip() or str(settings.get("team") or "")
    if not team:
        raise CliError(
            "a team submission names its team", ExitCode.USAGE, hint="pass --team <team>"
        )
    return team


def _person(settings: dict, repo: Path) -> str:
    """Who submits: AGENTIC_USER, the user config's `user`, else git's user.email."""
    for value in (os.environ.get("AGENTIC_USER", ""), str(settings.get("user") or "")):
        if value.strip():
            return value.strip()
    done = subprocess.run(
        ["git", "-C", str(repo), "config", "user.email"], capture_output=True, text=True
    )
    if done.returncode == 0 and done.stdout.strip():
        return done.stdout.strip()
    raise CliError(
        "who is submitting is unknown", ExitCode.REFUSED, hint="set git config user.email"
    )


def _target(home: Path, given: str) -> Path:
    path = Path(given)
    if path.is_dir():
        return path
    return promo.personal_folder(home, given)


def _all_personal(home: Path) -> list[Path]:
    root = home / "skills"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


def _show_validation(console, rows: list[dict]) -> None:
    if not rows:
        console.print("no personal skills (create one with `agentic skills new`)", markup=False)
    for row in rows:
        console.print(f"{row['name']}: {'ok' if not row['problems'] else 'problems'}", markup=False)
        for problem in row["problems"]:
            line = f"  {problem['check']} {problem['path']}: {problem['reason']}"
            console.print(line, markup=False)


def _show_pending(console, rows: list[dict]) -> None:
    if not rows:
        console.print("no open skill submissions", markup=False)
    for row in rows:
        team = f" ({row['team']})" if row.get("team") else ""
        console.print(
            f"#{row['number']} {row['skill']} -> {row['scope']}{team} by {row['submitter']}, "
            f"eval {row['verdict']} on {row['agent']} (as submitted, unverified until "
            f"approve checks the files): {row['url']}",
            markup=False,
        )


def _show_status(console, rows: list[dict]) -> None:
    if not rows:
        console.print("no submissions", markup=False)
    for row in rows:
        where = ""
        if row.get("retired_to"):
            where = f", personal copy moved to {row['retired_to']}"
        elif row.get("kept"):
            where = f", personal copy kept at {row['kept']}"
        console.print(f"{row['name']} -> {row['scope']}: {row['state']}{where}", markup=False)
