"""Which approved skills a user receives for a repo (master plan, "Skills init downloads").

  * the union of the resolved `skills` of every agent in the pipelines enabled for
    the repo (an agent's type supplies the skills it leaves out, D35);
  * plus every skill of an opt-in pack, only when the team enables that pack;
  * plus the authoring skills (`audience: admins`), only for an org admin or team lead
    - never for anyone else, even when an agent definition lists one.

Only catalogued skills are ever selected; anything else wanted is reported missing.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from agentic.definitions import AgentStep, DefinitionSet, LoopStep, PipelineDefinition
from agentic.definitions.discovery import UnknownDefinitionError
from agentic.policy import OrgPolicy, ResolveContext, resolve_agent
from agentic.skills.catalog import Catalog

ADMIN_ROLES = frozenset({"org-admin", "team-lead"})


@dataclass(frozen=True)
class Selection:
    names: tuple[str, ...]  # catalog keys to install, sorted
    missing: tuple[str, ...]  # wanted but not in the catalog: never installed


def pipeline_agents(pipeline: PipelineDefinition) -> list[str]:
    """The agents a pipeline runs, in step order (loops included), each once."""
    names: list[str] = []
    for step in pipeline.steps:
        inner = step.loop.steps if isinstance(step, LoopStep) else [step]
        names.extend(s.agent for s in inner if isinstance(s, AgentStep))
    return list(dict.fromkeys(names))


def pipeline_skills(
    definitions: DefinitionSet,
    pipelines: Iterable[str],
    policy: OrgPolicy,
    ctx: ResolveContext,
) -> list[str]:
    """The union of the resolved skills of every agent in `pipelines`, sorted. An agent
    that is not defined or does not resolve adds nothing: preflight refuses it."""
    wanted: set[str] = set()
    for pipeline_name in pipelines:
        for agent_name in pipeline_agents(definitions.pipeline(pipeline_name)):
            try:
                definition = definitions.agent(agent_name)
            except UnknownDefinitionError:
                continue
            resolved, _ = resolve_agent(definition, policy, ctx)
            if resolved is not None:
                wanted.update(resolved.skills)
    return sorted(wanted)


def team_packs(policy: OrgPolicy, team: str | None) -> list[str]:
    """The opt-in skill packs a team enabled (none for no team or an unknown team)."""
    team_policy = policy.teams.get(team) if team is not None else None
    return list(team_policy.skill_packs) if team_policy is not None else []


def select_skills(
    catalog: Catalog,
    wanted: Iterable[str],
    *,
    packs: Collection[str] = (),
    roles: Collection[str] = (),
) -> Selection:
    admin = bool(ADMIN_ROLES & set(roles))
    wanted = set(wanted)
    chosen: set[str] = set()
    for key, entry in catalog.skills.items():
        if entry.audience == "admins":
            if admin:
                chosen.add(key)
        elif entry.pack is not None:
            if entry.pack in packs:
                chosen.add(key)
        elif key in wanted:
            chosen.add(key)
    missing = sorted(key for key in wanted if key not in catalog.skills)
    return Selection(names=tuple(sorted(chosen)), missing=tuple(missing))
