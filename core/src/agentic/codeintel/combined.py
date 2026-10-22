"""Several code-intelligence adapters answering as one (M2-43, spec §10.1).

Agents can use both graph levels: the symbol graph (which functions and tests) and
the domain graph (which services, topics and tables, across repositories). `Combined`
asks every adapter that supports an operation, in order, and merges the answers:
files best first without repeats, notes joined, the source naming the adapters that
answered. Where the first adapter answers nothing the next one still does (DF-85). An
adapter that fails is named in the note and the others answer; only when every one
fails does the operation fail.
"""

from agentic.codeintel.interface import Candidates, CodeIntel, CodeIntelError


class Combined:
    def __init__(self, *adapters: CodeIntel) -> None:
        if not adapters:
            raise ValueError("Combined needs at least one adapter")
        self.adapters = adapters
        self.name = " + ".join(adapter.name for adapter in adapters)
        self.supports = frozenset().union(*(adapter.supports for adapter in adapters))

    @classmethod
    def of(cls, *adapters: CodeIntel) -> CodeIntel:
        """One adapter as itself, several as a `Combined`."""
        return adapters[0] if len(adapters) == 1 else cls(*adapters)

    def dependents(self, path: str) -> Candidates:
        return self._ask("dependents", path)

    def impact(self, diff: str) -> Candidates:
        return self._ask("impact", diff)

    def tests_for(self, path: str) -> Candidates:
        return self._ask("tests_for", path)

    def overview(self) -> str:
        parts, failures = [], []
        for adapter in self._supporting("overview"):
            try:
                parts.append(f"[{adapter.name}]\n{adapter.overview()}")
            except CodeIntelError as error:
                failures.append(f"[{adapter.name}] unavailable: {error}")
        if not parts:
            raise CodeIntelError("; ".join(failures) or "no adapter gives an overview")
        return "\n\n".join(parts + failures)

    def _supporting(self, op: str) -> list[CodeIntel]:
        return [adapter for adapter in self.adapters if op in adapter.supports]

    def _ask(self, op: str, arg: str) -> Candidates:
        files: dict[str, None] = {}
        notes, sources, failures = [], [], []
        for adapter in self._supporting(op):
            try:
                found = getattr(adapter, op)(arg)
            except CodeIntelError as error:
                failures.append(f"{adapter.name}: {error}")
                continue
            if found.files:
                sources.append(found.source)
            files.update(dict.fromkeys(found.files))
            if found.note:
                notes.append(found.note)
        if failures and len(failures) == len(self._supporting(op)):
            raise CodeIntelError("; ".join(failures))
        notes += [f"{failure} (no answer from it)" for failure in failures]
        source = " + ".join(sources) or self.name
        return Candidates(tuple(files), source=source, note="\n".join(notes))
