"""Which scope a stored proposal targets (M3-14, M3-15), read off its row.

One person's corrections recorded by that person's CLI (one author, one actor) are
**personal**; otherwise the proposal's cluster scope: **team** for `team:<team>`, else
**repo**. A person's edit (`kind`) may widen that, never narrow it."""

from __future__ import annotations

from agentic.learning.changes import SCOPE_OF


def narrowest_of(row) -> str:
    if len(set(row.authors)) == 1 and len({a.casefold() for a in row.actors}) == 1:
        return "personal"
    return "team" if row.scope.startswith("team:") else "repo"


def target_of(row) -> str:
    """The narrowest scope the evidence supports, or the edited kind's scope."""
    if row.kind and row.kind in SCOPE_OF:
        return SCOPE_OF[row.kind]
    return narrowest_of(row)


__all__ = ["narrowest_of", "target_of"]
