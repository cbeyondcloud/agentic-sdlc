"""Pipeline overlays: a team's changes to a pipeline for one repository (M2-23, spec §6 and
§6.1, D33).

Overlays live only in the policy repo, in the team's layer, so only that team's leads (and
org admins) change them through CODEOWNERS-reviewed pull requests:

    teams/<team>/pipelines/overlays/<repo>.yaml

`<repo>` is the repository's name - the last part of its `host/owner/name` origin key, in
any letter case. `repo: host/owner/name` binds the file to exactly one repository (two
repositories of the same name under different owners). A repository without an origin
gets no overlay: nothing could tell which one it is. One file may change several
pipelines:

    repo: git.example.com/acme/payments-api        # optional
    pipelines:
      feature-delivery:
        remove: [design]                           # steps, gates, loops not org-required
        add:
          - after: review                          # or before:; top level or in a loop
            step: {id: threat-model, agent: security-review}
        gates:
          review-approval: {four_eyes: true}       # approver_role, four_eyes (on), on_reject
        regime: regulated                          # only tighter: solo < team < regulated
        max_minutes: 90                            # only lower

An overlay never weakens a pipeline. Each refusal names its rule:

    required-step  removing an org-required step, gate or loop (or a loop holding one;
                   `org.yaml` required_steps)
    required-gate  changing an org-required gate's approver_role, or removing the step it
                   approves (its anchor: re-anchored, it would approve other work)
    four-eyes      turning four_eyes off
    regime         a looser regime
    max-minutes    a longer run limit
    unknown-step   naming a step, gate or loop the pipeline does not have (at that point:
                   removals apply first, then additions in order, then gate changes), or
                   an on_reject that is not an agent or action step before its gate
    empty-loop     removing every step of a loop (remove the loop instead)
    invalid        a result that is not a valid pipeline (a gate inside a loop, a name
                   used twice)

Gate re-anchoring (spec §6.1): a gate's anchor is the nearest step before it that is not
a gate - the work it approves. When an overlay removes a gate's anchor (never an
org-required gate's: rule required-gate), the gate stays and
anchors to the previous remaining step; with none before it, it moves right after the next
remaining step. A gate whose `on_reject` named a removed step routes rejections to the
step now standing in its place: the nearest remaining agent or action step before it (none:
the default, the step before the gate). Every change, re-anchoring included, is a note the
CLI shows with the run's plan.

`after: X` puts the new step straight after X, before any gate that follows X - that gate
then approves the new step's work too; `after: <the gate>` puts it after the gate."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agentic.definitions.errors import DefinitionError, describe_validation_error
from agentic.definitions.pipeline import STEP_KINDS, PipelineDefinition, PlainId, Step, step_kind
from agentic.yamlio import YamlFileError, load_yaml, read_text

#: Where a team's overlays live inside its layer (`teams/<team>/`).
OVERLAYS = Path("pipelines") / "overlays"
_REGIMES = ("solo", "team", "regulated")  # loosest to strictest


class OverlayRefused(DefinitionError):
    """An overlay file could not be read, or an overlay would weaken or break its
    pipeline; each line names the rule."""

    kind = "pipeline overlay"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Insert(_Strict):
    """One added step, placed straight after or before a named step."""

    after: PlainId | None = None
    before: PlainId | None = None
    step: Step

    @model_validator(mode="after")
    def one_place(self) -> Self:
        if (self.after is None) == (self.before is None):
            raise ValueError("an added step needs exactly one of after or before")
        return self


class GateChange(_Strict):
    approver_role: str | None = Field(default=None, min_length=1)
    four_eyes: bool | None = None  # only ever turned on
    on_reject: PlainId | None = None


class PipelineOverlay(_Strict):
    """What one overlay changes in one pipeline."""

    remove: list[PlainId] = []
    add: list[Insert] = []
    gates: dict[PlainId, GateChange] = {}
    regime: Literal["solo", "team", "regulated"] | None = None
    max_minutes: int | None = Field(default=None, ge=1)


class OverlayFile(_Strict):
    repo: str | None = Field(default=None, min_length=1)  # host/owner/name, when bound
    pipelines: dict[str, PipelineOverlay] = {}


@dataclass(frozen=True)
class Overlaid:
    """A pipeline with an overlay applied, and one note per change."""

    pipeline: PipelineDefinition
    notes: tuple[str, ...]


def load_overlay(path: Path) -> OverlayFile:
    """Read and validate one overlay file. Raises OverlayRefused."""
    try:
        data, lines = load_yaml(read_text(path))
    except YamlFileError as error:
        raise OverlayRefused(path, [str(error)]) from error
    try:
        return OverlayFile.model_validate(data if data is not None else {})
    except ValidationError as error:
        raise OverlayRefused.from_validation_error(
            path, error, "overlay", union_tags=STEP_KINDS, lines=lines
        ) from error


def overlay_applies(stem: str, repo: str | None, repo_key: str | None) -> bool:
    """Whether the overlay file `<stem>.yaml` (binding `repo`, or None) applies to the
    repository whose origin key is `repo_key` (`host/owner/name`; None without origin)."""
    if not repo_key:
        return False
    if stem.casefold() != repo_key.rstrip("/").rsplit("/", 1)[-1].casefold():
        return False
    return repo is None or repo.casefold() == repo_key.casefold()


def approved_overlays(
    index: Mapping[str, Mapping[str, Any]], *, team: str, repo: str | None, pipeline: str
) -> list[str]:
    """The overlays of `index` (`agentic.policy.load.overlay_index`: POSIX path -> its
    `team`, bound `repo` and `pipelines`) that apply to a run of `team` on `pipeline` in
    the repository whose origin key is `repo`: what the control plane expects a run to
    have applied (M2-23, D33)."""
    return [
        rel
        for rel, entry in sorted(index.items())
        if entry.get("team") == team
        and pipeline in (entry.get("pipelines") or ())
        and overlay_applies(PurePosixPath(rel).stem, entry.get("repo"), repo)
    ]


def overlay_for(team_root: Path | None, repo_key: str | None) -> Path | None:
    """The team's overlay file for this repository, or None. Two files applying to one
    repository, or a stray file in the folder, are errors: either would hide an overlay
    its author expects to apply."""
    if team_root is None or not repo_key:
        return None
    folder = team_root / OVERLAYS
    if not folder.is_dir():
        return None
    entries = sorted(entry for entry in folder.iterdir() if not entry.name.startswith("."))
    stray = [entry.name for entry in entries if not (entry.is_file() and entry.suffix == ".yaml")]
    if stray:
        raise OverlayRefused(
            folder, [f"unexpected entries (only *.yaml files): {', '.join(stray)}"]
        )
    found = [
        path
        for path in entries
        if overlay_applies(path.stem, None, repo_key)
        and overlay_applies(path.stem, load_overlay(path).repo, repo_key)
    ]
    if len(found) > 1:
        raise OverlayRefused(
            folder,
            [f"{len(found)} overlays apply to {repo_key}: {', '.join(p.name for p in found)}"],
        )
    return found[0] if found else None


# ── applying an overlay ──────────────────────────────────────────────────────


def _name(step: dict) -> str:
    kind = step_kind(step)
    if kind == "gate":
        return step["gate"]
    if kind == "loop":
        return step["loop"]["id"]
    return step["id"]


def _inner(step: dict) -> list[dict]:
    return step["loop"]["steps"] if step_kind(step) == "loop" else []


def _covered(step: dict) -> list[str]:
    """Every name a step contributes: a loop's own and its inner steps'."""
    return [_name(step), *(_name(inner) for inner in _inner(step))]


def _locate(steps: list[dict], name: str) -> tuple[list[dict], int] | None:
    """The list holding the step named `name` (the top level or a loop's) and its index."""
    for index, step in enumerate(steps):
        if _name(step) == name:
            return steps, index
        for inner_index, inner in enumerate(_inner(step)):
            if _name(inner) == name:
                return _inner(step), inner_index
    return None


def _anchor(steps: list[dict], index: int) -> str | None:
    """The gate at `index`'s anchor: the nearest step before it that is not a gate."""
    for step in reversed(steps[:index]):
        if step_kind(step) != "gate":
            return _name(step)
    return None


def _work_steps(steps: list[dict]) -> list[str]:
    """Agent and action step names in pipeline order, loop bodies included."""
    out = []
    for step in steps:
        if step_kind(step) == "loop":
            out += [_name(inner) for inner in _inner(step)]
        elif step_kind(step) != "gate":
            out.append(_name(step))
    return out


def apply_overlay(
    base: PipelineDefinition,
    overlay: PipelineOverlay,
    required: Collection[str] = (),
    *,
    source: Path | None = None,
) -> Overlaid:
    """`base` with `overlay` applied (the base is not changed). `required` are the
    pipeline's org-required names. Raises OverlayRefused naming each broken rule."""
    where = source if source is not None else Path(f"{base.name} overlay")
    required = set(required)
    data: dict[str, Any] = base.model_dump(mode="json")
    steps: list[dict] = data["steps"]
    problems: list[str] = []
    notes: list[str] = []

    _remove(base.name, steps, overlay.remove, required, problems, notes)
    for insert in overlay.add:
        _add(steps, insert, problems, notes)
    for name, change in overlay.gates.items():
        _change_gate(steps, name, change, required, problems, notes)
    if overlay.regime is not None and overlay.regime != base.regime:
        if _REGIMES.index(overlay.regime) < _REGIMES.index(base.regime):
            problems.append(
                f"regime: rule regime: an overlay may only tighten the regime "
                f"({base.regime} -> {overlay.regime} is looser)"
            )
        else:
            data["regime"] = overlay.regime
            notes.append(f"regime {base.regime} -> {overlay.regime}")
    if overlay.max_minutes is not None and overlay.max_minutes != base.max_minutes:
        if base.max_minutes is not None and overlay.max_minutes > base.max_minutes:
            problems.append(
                f"max_minutes: rule max-minutes: an overlay may only lower the run limit "
                f"({base.max_minutes} -> {overlay.max_minutes})"
            )
        else:
            data["max_minutes"] = overlay.max_minutes
            notes.append(f"max_minutes {base.max_minutes} -> {overlay.max_minutes}")
    if problems:
        raise OverlayRefused(where, problems)
    try:
        pipeline = PipelineDefinition.model_validate(data)
    except ValidationError as error:
        lines = describe_validation_error(error, "pipeline", union_tags=STEP_KINDS)
        raise OverlayRefused(
            where,
            [f"result: rule invalid: the overlaid pipeline is not valid: {line}" for line in lines],
        ) from error
    return Overlaid(pipeline, tuple(notes))


def _remove(
    pipeline: str,
    steps: list[dict],
    names: list[str],
    required: set[str],
    problems: list[str],
    notes: list[str],
) -> None:
    """Remove `names`, then re-anchor every gate whose anchor went and move every
    `on_reject` that named a removed step."""
    anchors = {
        _name(step): _anchor(steps, index)
        for index, step in enumerate(steps)
        if step_kind(step) == "gate"
    }
    order = _work_steps(steps)
    approved_by = {anchor: gate for gate, anchor in anchors.items() if gate in required and anchor}
    removed: set[str] = set()
    for name in names:
        found = _locate(steps, name)
        if found is None:
            problems.append(
                f"remove {name}: rule unknown-step: {pipeline} has no step, gate or loop "
                f"named {name!r}"
            )
            continue
        holder, index = found
        hit = [covered for covered in _covered(holder[index]) if covered in required]
        if hit:
            what = name if hit == [name] else f"{name} (it holds {', '.join(hit)})"
            problems.append(
                f"remove {what}: rule required-step: org policy requires "
                f"{', '.join(hit)} in {pipeline} (org.yaml required_steps)"
            )
            continue
        if name in approved_by:
            problems.append(
                f"remove {name}: rule required-gate: org-required gate {approved_by[name]} "
                f"approves {name}'s work; re-anchored it would approve another step "
                "(org.yaml required_steps)"
            )
            continue
        removed.update(_covered(holder[index]))
        del holder[index]
        notes.append(f"removed {name}")
    for step in steps:
        if step_kind(step) == "loop" and not _inner(step):
            problems.append(
                f"remove: rule empty-loop: loop {_name(step)} would have no steps left; "
                "remove the loop instead"
            )
    if not removed:
        return
    _reanchor(steps, anchors, removed, problems, notes)
    for step in steps:
        target = step.get("on_reject") if step_kind(step) == "gate" else None
        if target in removed:
            before = order[: order.index(target)] if target in order else []
            standing = next((n for n in reversed(before) if n not in removed), None)
            step["on_reject"] = standing
            to = standing or "the step before the gate"
            notes.append(f"gate {_name(step)} on_reject moved from {target} to {to}")


def _reanchor(
    steps: list[dict],
    anchors: dict[str, str | None],
    removed: set[str],
    problems: list[str],
    notes: list[str],
) -> None:
    head: list[dict] = []  # gates left with nothing before them, in order
    for index, step in enumerate(steps):
        name = _name(step)
        if step_kind(step) != "gate" or anchors.get(name) not in removed:
            continue
        new = _anchor(steps, index)
        if new is not None:
            notes.append(f"gate {name} re-anchored from {anchors[name]} to {new}")
        else:
            head.append(step)
    if not head:
        return
    for step in head:
        steps.remove(step)
    following = next((i for i, step in enumerate(steps) if step_kind(step) != "gate"), None)
    if following is None:
        problems.append(
            f"remove: rule invalid: gate {', '.join(_name(s) for s in head)} would gate "
            "nothing - no step is left"
        )
        steps[:0] = head
        return
    steps[following + 1 : following + 1] = head
    for step in head:
        notes.append(
            f"gate {_name(step)} re-anchored from {anchors[_name(step)]} to "
            f"{_name(steps[following])} (moved after it)"
        )


def _add(steps: list[dict], insert: Insert, problems: list[str], notes: list[str]) -> None:
    step = insert.step.model_dump(mode="json")
    name, target = _name(step), insert.after or insert.before
    assert target is not None  # Insert.one_place
    found = _locate(steps, target)
    if found is None:
        problems.append(f"add {name}: rule unknown-step: no step, gate or loop named {target!r}")
        return
    holder, index = found
    if holder is not steps and step_kind(step) in ("gate", "loop"):
        problems.append(
            f"add {name}: rule invalid: {target} is inside a loop, and a loop cannot hold "
            "a gate or another loop"
        )
        return
    holder.insert(index + 1 if insert.after else index, step)
    notes.append(f"added {name} {'after' if insert.after else 'before'} {target}")


def _change_gate(
    steps: list[dict],
    name: str,
    change: GateChange,
    required: set[str],
    problems: list[str],
    notes: list[str],
) -> None:
    index = next(
        (i for i, s in enumerate(steps) if step_kind(s) == "gate" and s["gate"] == name), None
    )
    if index is None:
        problems.append(f"gates {name}: rule unknown-step: no gate named {name!r}")
        return
    gate = steps[index]
    if change.approver_role is not None and change.approver_role != gate["approver_role"]:
        if name in required:
            problems.append(
                f"gates {name}: rule required-gate: the approver of an org-required gate "
                "cannot change (org.yaml required_steps)"
            )
        else:
            notes.append(
                f"gate {name} approver_role {gate['approver_role']} -> {change.approver_role}"
            )
            gate["approver_role"] = change.approver_role
    if change.four_eyes is not None and change.four_eyes != gate["four_eyes"]:
        if not change.four_eyes:
            problems.append(f"gates {name}: rule four-eyes: an overlay cannot turn four_eyes off")
        else:
            gate["four_eyes"] = True
            notes.append(f"gate {name} four_eyes on")
    if change.on_reject is not None and change.on_reject != gate.get("on_reject"):
        if change.on_reject not in _work_steps(steps[:index]):
            problems.append(
                f"gates {name}: rule unknown-step: on_reject names {change.on_reject!r}, "
                "which is not an agent or action step before the gate"
            )
            return
        gate["on_reject"] = change.on_reject
        notes.append(f"gate {name} on_reject -> {change.on_reject}")
