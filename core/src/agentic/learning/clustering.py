"""Repeated corrections become proposals (M3-13, spec §24 "Detect repeats").

`propose` is a pure function: lessons and the proposals made so far in, the proposals to
create or update out. The same input gives the same clusters and ids on every machine.

  * **Window.** Only lessons from the last `window_days` count (default 30).
  * **Clusters, per agent and scope.** A lesson joins the first cluster (in time order)
    whose seed - its first lesson - it resembles (`alike`: a `Similarity` score, default
    lexical, above the policy's `similarity`); otherwise it seeds a new one. Seed-based
    rather than chained, so one bridging lesson cannot merge two unrelated clusters.
  * **Threshold.** A cluster is a pattern when it holds `threshold` distinct corrections
    (default 3): distinct `source`s, so one pull request comment captured as a review
    comment and as a review miss counts once.
  * **Narrowest scope.** Repo first. A team-wide cluster (the team's repos together)
    becomes a team proposal only when no single repo holds enough of it on its own - its
    evidence comes from at least two repos.
  * **Rejected lessons are remembered.** A cluster similar to a rejected (or reverted)
    proposal of the same agent and scope is proposed again only when its corrections made
    after that decision reach the threshold by themselves; that proposal gets a new id.
  * **Decided proposals are not proposed twice.** An approved, deferred or applied
    proposal is matched like a pending one, but its evidence stays as it was decided.
  * **Pending proposals grow.** A cluster matching a pending proposal updates its
    evidence instead of making a second one. It matches by id, and - since the id comes
    from the cluster's first lesson, which changes when an older correction arrives late
    or the first one leaves the window - by shared evidence or a lesson alike the
    proposal's, in the same agent and scope. A pending proposal keeps its id and lesson.

Creating a proposal changes nothing else: what it proposes is applied only after a person
approves it (L4-L6, M3-14..M3-16; D29).
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from agentic.learning.lessons import LexicalSimilarity, Similarity, alike, tokens

#: A proposal's life (M3-15, M3-16; `agentic.learning.approval.TRANSITIONS`): pending ->
#: approved -> applied -> reverted, or pending -> rejected, or pending -> deferred.
ProposalStatus = Literal["pending", "approved", "rejected", "deferred", "applied", "reverted"]
#: A person said no to the lesson (or took it back): proposed again only on new evidence.
REMEMBERED = ("rejected", "reverted")
#: The same pattern is already decided or waiting on a person: matched, never duplicated.
SETTLED = ("approved", "deferred", "applied")


@dataclass(frozen=True)
class LearningSettings:
    """The policy's learning thresholds for one team (`OrgPolicy.learning`). A learner the
    Learning Analyst named with less than `min_confidence` does not count (DF-732)."""

    threshold: int = 3
    window_days: int = 30
    similarity: float = 0.5
    min_confidence: float = 0.5


@dataclass(frozen=True)
class Lesson:
    """One attributed record, as one agent's lesson."""

    feedback_id: str
    agent: str
    text: str
    at: datetime
    source: str
    actor: str
    team: str | None = None
    repo: str | None = None
    confidence: float = 1.0  # the attribution's confidence for this agent

    @property
    def repo_scope(self) -> str:
        return f"repo:{self.repo or 'local'}"

    @property
    def team_scope(self) -> str | None:
        return f"team:{self.team}" if self.team else None


@dataclass(frozen=True)
class Proposal:
    id: str
    agent: str
    scope: str  # "repo:<host/owner/name>" or "team:<team>"
    lesson: str
    evidence: tuple[str, ...]  # feedback ids, oldest first
    sources: tuple[str, ...]
    actors: tuple[str, ...]
    created_at: datetime
    status: ProposalStatus = "pending"
    decided_at: datetime | None = None
    decided_by: str | None = None
    endorsements: tuple[str, ...] = ()  # people who back it (weight, never a decision)


@dataclass
class _Cluster:
    seed: Lesson
    members: list[Lesson] = field(default_factory=list)

    def sources(self, after: datetime | None = None) -> set[str]:
        return {m.source for m in self.members if after is None or m.at > after}


def proposal_id(agent: str, scope: str, lesson: str, generation: int = 0) -> str:
    material = "\x00".join((agent, scope, " ".join(sorted(tokens(lesson))), str(generation)))
    return "lp-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _clusters(lessons: Iterable[Lesson], similarity: Similarity) -> list[_Cluster]:
    found: list[_Cluster] = []
    for item in sorted(lessons, key=lambda x: (x.at, x.feedback_id)):
        home = next((c for c in found if alike(similarity, c.seed.text, item.text)), None)
        if home is None:
            found.append(_Cluster(item, [item]))
        else:
            home.members.append(item)
    return found


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def propose(
    lessons: Sequence[Lesson],
    settings: LearningSettings,
    now: datetime,
    *,
    existing: Sequence[Proposal] = (),
    similarity: Similarity | None = None,
) -> list[Proposal]:
    """The proposals to create, and the pending ones whose evidence grew."""
    similarity = similarity or LexicalSimilarity(settings.similarity)
    start = now - timedelta(days=settings.window_days)
    recent = [
        item
        for item in lessons
        if start <= item.at <= now and item.confidence >= settings.min_confidence
    ]
    by_id = {p.id: p for p in existing}
    pending = sorted(
        (p for p in existing if p.status == "pending" or p.status in SETTLED),
        key=lambda p: (p.created_at, p.id),
    )
    out: dict[str, Proposal] = {}  # by id: one cluster's update never hides another's

    def held_for(agent: str, scope: str, pid: str, members: list[Lesson]) -> Proposal | None:
        """The pending proposal this pattern already is: the same id, or - when the
        cluster's first lesson changed (an older correction arrived late, the first one
        left the window) - one of the same agent and scope sharing evidence with it or
        alike its lesson. A proposal keeps its id for as long as it is pending."""
        if pid in by_id:
            return by_id[pid]
        ids = {m.feedback_id for m in members}
        for p in pending:
            if (p.agent, p.scope) != (agent, scope):
                continue
            if ids & set(p.evidence) or any(alike(similarity, p.lesson, m.text) for m in members):
                return p
        return None

    def consider(agent: str, scope: str, cluster: _Cluster) -> bool:
        """Propose `cluster` if it is a pattern; True when it is one (made or not)."""
        rejected = [
            p
            for p in existing
            if p.status in REMEMBERED
            and (p.agent, p.scope) == (agent, scope)
            and alike(similarity, p.lesson, cluster.seed.text)
        ]
        after = max((p.decided_at or p.created_at for p in rejected), default=None)
        members = [m for m in cluster.members if after is None or m.at > after]
        if len({m.source for m in members}) < settings.threshold:
            return False
        seed = members[0]
        pid = proposal_id(agent, scope, seed.text, generation=len(rejected))
        made = Proposal(
            id=pid,
            agent=agent,
            scope=scope,
            lesson=seed.text,
            evidence=_unique(m.feedback_id for m in members),
            sources=_unique(m.source for m in members),
            actors=_unique(m.actor for m in members),
            created_at=now,
        )
        held = held_for(agent, scope, pid, members)
        if held is None:
            out.setdefault(pid, made)
            return True
        held = out.get(held.id, held)
        if held.status == "pending" and set(made.evidence) - set(held.evidence):
            evidence = _unique([*held.evidence, *made.evidence])
            sources = _unique([*held.sources, *made.sources])
            actors = _unique([*held.actors, *made.actors])
            out[held.id] = replace(held, evidence=evidence, sources=sources, actors=actors)
        return True

    groups: dict[tuple[str, str], list[Lesson]] = {}
    for item in recent:
        groups.setdefault((item.agent, item.repo_scope), []).append(item)
    for (agent, scope), members in sorted(groups.items()):
        for cluster in _clusters(members, similarity):
            consider(agent, scope, cluster)

    teams: dict[tuple[str, str], list[Lesson]] = {}
    for item in recent:
        if item.team_scope:
            teams.setdefault((item.agent, item.team_scope), []).append(item)
    for (agent, scope), members in sorted(teams.items()):
        for cluster in _clusters(members, similarity):
            per_repo: dict[str, set[str]] = {}
            for m in cluster.members:
                per_repo.setdefault(m.repo_scope, set()).add(m.source)
            if len(per_repo) < 2 or max(len(s) for s in per_repo.values()) >= settings.threshold:
                continue  # one repo: the repo proposal is the narrower one
            consider(agent, scope, cluster)
    return list(out.values())


__all__ = [
    "REMEMBERED",
    "SETTLED",
    "LearningSettings",
    "Lesson",
    "Proposal",
    "ProposalStatus",
    "proposal_id",
    "propose",
]
