"""What the org and the repository define (M1-56; spec §5.5, §5.7, §12.6, §24 step 5):

agentic validate [PATH...]                 agents, pipelines and policy, field by field
agentic agents list | search <text>        agents of every layer (org, team, repo)
agentic agents new <name> --type <type>    a repo agent from its type's template (D35)
agentic pipelines list | search <text>
agentic pipelines new <name>               a repo pipeline from the pipeline template
agentic skills list | search <text>        the org skills catalog, and what is installed
agentic policy show                        tiers, classes, agent types and caps, the team
"""

import re
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer

from agentic.definitions import (
    AgentDefinition,
    DefinitionError,
    PipelineDefinition,
    load_agent,
    load_pipeline,
)
from agentic.engine.preflight import preflight
from agentic.policy import OrgPolicy, resolve_agent
from agentic.skills.catalog import CatalogError, load_catalog, skill_digest, skill_folder
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.output import table as output_table
from agentic_cli.runtime import ACTIONS, CONFIGURED_BACKENDS, installed_skills_root
from agentic_cli.workspace import Workspace

_SLUG = re.compile(r"[a-z][a-z0-9-]{0,63}")


def register(app: typer.Typer) -> None:
    agents = typer.Typer(help="List, search and create agents.", no_args_is_help=True)
    pipelines = typer.Typer(help="List, search and create pipelines.", no_args_is_help=True)
    skills = typer.Typer(help="List and search the org skills catalog.", no_args_is_help=True)
    policy = typer.Typer(help="Show the org policy that applies here.", no_args_is_help=True)
    app.add_typer(agents, name="agents")
    app.add_typer(pipelines, name="pipelines")
    app.add_typer(skills, name="skills")
    app.add_typer(policy, name="policy")

    @app.command("validate")
    def validate(
        ctx: typer.Context,
        paths: Annotated[
            list[Path] | None, typer.Argument(help="Agent (.md) or pipeline (.yaml) files.")
        ] = None,
    ) -> None:
        """Check agents and pipelines against org policy before use (exit 3 on an error;
        warnings are shown and pass, as they do for a run)."""
        out: Output = ctx.obj
        ws = Workspace.load()
        report = _validate(ws, paths or [])
        out.result(report, _show_validation(report))
        errors = sum(p["severity"] != "warning" for p in report["problems"])
        if errors:
            raise CliError(f"validation failed: {_counted(report)}", ExitCode.INVALID)

    @agents.command("list")
    def agents_list(ctx: typer.Context) -> None:
        """Every agent visible here; a nearer layer's agent replaces a farther one."""
        _agents(ctx.obj, Workspace.load(), "")

    @agents.command("search")
    def agents_search(ctx: typer.Context, text: str) -> None:
        """Agents whose name, type or description contains TEXT."""
        _agents(ctx.obj, Workspace.load(), text)

    @agents.command("new")
    def agents_new(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="lowercase-with-hyphens"),
        agent_type: str = typer.Option(..., "--type", help="An agent type of your org."),
    ) -> None:
        """Create .agentic/agents/NAME.md from the type's template (D35)."""
        out: Output = ctx.obj
        _check_slug(name)
        ws = Workspace.load()
        types = ws.policy().agent_types
        if agent_type not in types:
            raise CliError(
                f"no agent type {agent_type!r} in org policy",
                ExitCode.INVALID,
                hint=f"known types: {', '.join(sorted(types))}",
            )
        org_template = (ws.policy_repo or Path()) / "templates" / "agents" / f"{agent_type}.md"
        if org_template.is_file():
            text, source = org_template.read_text(encoding="utf-8"), str(org_template)
        else:
            text = files("agentic_cli").joinpath("templates", "agent.md").read_text("utf-8")
            source = "the built-in agent template"
        text = _set_key(_set_key(text, "name", name), "type", agent_type)
        agent = _checked_agent(name, text, source)  # nothing is written from a broken template
        path = _create(ws.repo / ".agentic" / "agents" / f"{name}.md", text)
        problems = _agent_problems(ws, agent, path)
        if name in ws.definitions().agents:
            out.warn(f"{name} also exists in the {ws.definitions().agent_source(name)} layer")
        out.result(
            {"path": str(path), "problems": problems},
            lambda c: c.print(f"created {path}", markup=False),
        )

    @pipelines.command("list")
    def pipelines_list(ctx: typer.Context) -> None:
        """Every pipeline visible here."""
        _pipelines(ctx.obj, Workspace.load(), "")

    @pipelines.command("search")
    def pipelines_search(ctx: typer.Context, text: str) -> None:
        """Pipelines whose name contains TEXT."""
        _pipelines(ctx.obj, Workspace.load(), text)

    @pipelines.command("new")
    def pipelines_new(ctx: typer.Context, name: str) -> None:
        """Create .agentic/pipelines/NAME.yaml from the pipeline template."""
        out: Output = ctx.obj
        _check_slug(name)
        ws = Workspace.load()
        text = files("agentic_cli").joinpath("templates", "pipeline.yaml").read_text("utf-8")
        target = ws.repo / ".agentic" / "pipelines" / f"{name}.yaml"
        path = _create(target, _set_key(text, "name", name))
        if not ws.policy().allow_repo_pipelines:
            out.warn(
                "org policy does not run repo pipelines (allow_repo_pipelines: false, D33): "
                "propose this one to the policy repo"
            )
        out.result({"path": str(path)}, lambda c: c.print(f"created {path}", markup=False))

    @skills.command("list")
    def skills_list(ctx: typer.Context) -> None:
        """Every skill in the org catalog, and whether it is installed as pinned."""
        _skills(ctx.obj, Workspace.load(), "")

    @skills.command("search")
    def skills_search(ctx: typer.Context, text: str) -> None:
        """Skills whose key or notes contain TEXT."""
        _skills(ctx.obj, Workspace.load(), text)

    @policy.command("show")
    def policy_show(ctx: typer.Context) -> None:
        """Tiers and models, repo classes, agent types and caps, and your team's narrowing."""
        out: Output = ctx.obj
        ws = Workspace.load()
        shown = _policy_summary(ws.policy(), ws.team)
        out.result(shown, lambda c: _show_policy(c, shown))


# ── validate ─────────────────────────────────────────────────────────────────


def _validate(ws: Workspace, paths: list[Path]) -> dict:
    problems: list[dict] = []
    agents = pipelines = 0
    if not paths:
        definitions = ws.definitions()
        for name, (agent, layer) in sorted(definitions.agents.items()):
            agents += 1
            problems += _agent_problems(ws, agent, definitions.agent_file(name) or Path(layer))
        for name in definitions.pipeline_names():
            pipelines += 1
            where = definitions.pipeline_file(name) or Path(name)
            problems += _pipeline_problems(ws, definitions.pipeline(name), where)
    for path in paths:
        try:
            if path.suffix == ".md":
                agents += 1
                problems += _agent_problems(ws, load_agent(path), path)
            else:
                pipelines += 1
                problems += _pipeline_problems(ws, load_pipeline(path), path)
        except DefinitionError as error:
            for line in error.errors:
                field, _, reason = line.partition(": ")
                problems.append(_problem(path, path.stem, field, reason or line))
    return {"agents": agents, "pipelines": pipelines, "problems": problems}


def _agent_problems(ws: Workspace, agent: AgentDefinition, where: Path) -> list[dict]:
    _, violations = resolve_agent(agent, ws.policy(), ws.context())
    return [_problem(where, agent.name, v.field, v.reason, v.allowed) for v in violations]


def _pipeline_problems(ws: Workspace, pipeline: PipelineDefinition, where: Path) -> list[dict]:
    report = preflight(
        pipeline,
        ws.definitions(),
        ws.policy(),
        ws.context(),
        actions=ACTIONS,
        available_backends=CONFIGURED_BACKENDS,
    )
    return [
        _problem(where, p.step or pipeline.name, p.check, p.detail, p.policy_value, p.severity)
        for p in report.problems
    ]


def _problem(where, name, field, reason, allowed=None, severity="error") -> dict:
    return {
        "file": str(where),
        "name": name,
        "field": field,
        "reason": reason,
        "allowed": allowed,
        "severity": severity,
    }


def _counted(report: dict) -> str:
    """'2 errors, 1 warning'."""
    warnings = sum(p["severity"] == "warning" for p in report["problems"])
    errors = len(report["problems"]) - warnings
    return (
        f"{errors} error{'s' if errors != 1 else ''}, "
        f"{warnings} warning{'s' if warnings != 1 else ''}"
    )


def _show_validation(report: dict):
    def show(console) -> None:
        agents, pipelines = report["agents"], report["pipelines"]
        head = (
            f"{agents} agent{'s' if agents != 1 else ''}, "
            f"{pipelines} pipeline{'s' if pipelines != 1 else ''}"
        )
        if not report["problems"]:
            console.print(f"valid: {head}", markup=False)
            return
        console.print(f"checked {head}: {_counted(report)}", markup=False)
        for p in report["problems"]:
            allowed = f" (allowed: {p['allowed']})" if p["allowed"] not in (None, []) else ""
            warning = " (warning)" if p["severity"] == "warning" else ""
            console.print(
                f"  {p['file']}: {p['name']}: {p['field']}{warning}: {p['reason']}{allowed}",
                markup=False,
            )

    return show


# ── listing ──────────────────────────────────────────────────────────────────


def _matches(text: str, *fields: object) -> bool:
    needle = text.strip().lower()
    return not needle or any(needle in str(f or "").lower() for f in fields)


def _agents(out: Output, ws: Workspace, text: str) -> None:
    rows = [
        {
            "name": name,
            "type": agent.type,
            "version": agent.version,
            "layer": layer,
            "description": agent.description,
            "status": agent.status or "active",
        }
        for name, (agent, layer) in sorted(ws.definitions().agents.items())
        if _matches(text, name, agent.type, agent.description)
    ]
    out.result(rows, lambda c: _table(c, rows, ("name", "type", "version", "layer", "description")))


def _pipelines(out: Output, ws: Workspace, text: str) -> None:
    definitions = ws.definitions()
    rows = [
        {
            "name": name,
            "version": definitions.pipeline(name).version,
            "regime": definitions.pipeline(name).regime,
            "layer": definitions.pipeline_source(name),
            "steps": len(definitions.pipeline(name).steps),
        }
        for name in definitions.pipeline_names()
        if _matches(text, name)
    ]
    out.result(rows, lambda c: _table(c, rows, ("name", "version", "regime", "layer", "steps")))


def _skills(out: Output, ws: Workspace, text: str) -> None:
    root = (ws.policy_repo or Path()) / "skills"
    if not (root / "catalog.yaml").is_file():
        out.warn(f"no skills catalog at {root / 'catalog.yaml'}")
        out.result([], lambda c: None)
        return
    try:
        catalog = load_catalog(root)
    except CatalogError as error:
        raise CliError(str(error), ExitCode.INVALID) from error
    installed = installed_skills_root(ws)
    rows = []
    for key, entry in sorted(catalog.skills.items()):
        if not _matches(text, key, entry.notes):
            continue
        folder = skill_folder(installed, key)
        rows.append(
            {
                "key": key,
                "version": entry.version,
                "loading": entry.loading,
                "licence": entry.licence,
                "audience": entry.audience,
                "installed": folder.is_dir() and skill_digest(folder) == entry.sha256,
            }
        )
    columns = ("key", "version", "loading", "licence", "installed")
    out.result(rows, lambda c: _table(c, rows, columns))


def _table(console, rows: list[dict], columns: tuple[str, ...]) -> None:
    if not rows:
        console.print("nothing found", markup=False)
        return
    table = output_table(*columns)
    for row in rows:
        table.add_row(*(str(row[c]) for c in columns))
    console.print(table)


# ── creating ─────────────────────────────────────────────────────────────────


def _check_slug(name: str) -> None:
    if not _SLUG.fullmatch(name):
        raise CliError(
            f"{name!r} is not a name: lowercase letters, digits and hyphens, starting with a "
            "letter",
            ExitCode.USAGE,
        )


def _set_key(text: str, key: str, value: str) -> str:
    """Set the first `key:` line's value, keeping its comment."""
    return re.sub(rf"^({key}:[ \t]*)[^\s#]+", rf"\g<1>{value}", text, count=1, flags=re.M)


def _checked_agent(name: str, text: str, source: str) -> AgentDefinition:
    """The agent `text` defines, loaded from a scratch copy; a template that does not give
    a valid agent is refused before anything is written to the repository."""
    with tempfile.TemporaryDirectory(prefix="agentic-new-") as scratch:
        draft = Path(scratch) / f"{name}.md"
        draft.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
        try:
            return load_agent(draft)
        except DefinitionError as error:
            reasons = "; ".join(error.errors) or str(error)
            raise CliError(
                f"the agent template does not give a valid agent ({reasons}); nothing was written",
                ExitCode.INVALID,
                hint=f"fix {source} (in the policy repo, by a pull request)",
            ) from error


def _create(path: Path, text: str) -> Path:
    if path.exists():
        raise CliError(f"{path} already exists", ExitCode.REFUSED, hint="choose another name")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    return path


# ── policy ───────────────────────────────────────────────────────────────────


def _policy_summary(policy: OrgPolicy, team: str | None) -> dict:
    team_policy = policy.teams.get(team) if team else None
    overrides = policy.team_overrides.get(team, {}) if team else {}
    return {
        "version": policy.version,
        "default_classification": policy.default_classification,
        "tiers": {
            name: {
                "model": (overrides.get(name) or spec).model,
                "effort": spec.effort,
                "temperature": spec.temperature,
            }
            for name, spec in policy.tiers.items()
        },
        "classifications": {
            name: rule.model_dump(mode="json") for name, rule in policy.classifications.items()
        },
        "agent_types": {
            name: {
                "tier": t.tier,
                "backends": t.backends,
                "tools": t.tools,
                "produces": t.produces,
                "caps": t.limits.max.model_dump(mode="json"),
                "children_max_total": t.limits.children_max.max_total,
            }
            for name, t in policy.agent_types.items()
        },
        "team": None
        if team_policy is None
        else {"name": team, **team_policy.model_dump(mode="json", exclude={"deny"})},
        "allow_repo_pipelines": policy.allow_repo_pipelines,
        "sandbox_required_for": policy.sandbox.required_for,
        "forbidden_tools": policy.forbidden_tools,
    }


def _show_policy(console, shown: dict) -> None:
    console.print(f"policy {shown['version']}", markup=False)
    tiers = output_table("tier", "model", "effort")
    for name, tier in shown["tiers"].items():
        tiers.add_row(name, tier["model"], str(tier["effort"] or ""))
    console.print(tiers)
    columns = ("agent type", "tier", "backends", "budget USD", "turns", "minutes", "children")
    types = output_table(*columns)
    for name, t in shown["agent_types"].items():
        caps = t["caps"]
        types.add_row(
            name,
            t["tier"],
            ", ".join(t["backends"]),
            str(caps["budget_usd"]),
            str(caps["max_turns"]),
            str(caps["max_minutes"]),
            str(t["children_max_total"]),
        )
    console.print(types)
    classes = ", ".join(shown["classifications"])
    console.print(f"repo classes: {classes} (default {shown['default_classification']})")
    if shown["team"]:
        team = shown["team"]
        tiers_allowed = team.get("allowed_tiers") or "every tier"
        console.print(f"team {team['name']}: tiers {tiers_allowed}", markup=False)
