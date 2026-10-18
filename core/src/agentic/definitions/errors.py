"""Errors raised while loading agent and pipeline definition files."""

from pathlib import Path

from pydantic import ValidationError


class DefinitionError(Exception):
    """A definition file could not be loaded or failed validation.

    Carries the file path and one readable line per problem, so the CLI can
    show the user exactly what to fix.
    """

    kind = "definition"  # subclasses name what was invalid, e.g. "policy"

    def __init__(self, path: Path, errors: list[str]) -> None:
        self.path = path
        self.errors = errors
        bullet_list = "\n".join(f"  - {error}" for error in errors)
        super().__init__(f"{path}: invalid {self.kind}\n{bullet_list}")

    @classmethod
    def from_validation_error(
        cls,
        path: Path,
        error: ValidationError,
        whole_file_label: str,
        union_tags: frozenset[str] = frozenset(),
    ) -> "DefinitionError":
        """Build from a pydantic error: one '<field>: <what is wrong>' line per issue.

        whole_file_label names checks that concern the whole file rather than one
        field (pydantic reports those with no field name), e.g. "agent" or "pipeline".

        union_tags are the tags of a tagged union (e.g. the pipeline step kinds).
        Pydantic inserts the chosen tag into the error path right after the list
        position ("steps.3.loop.loop.max_iterations"); those are removed so the path
        reads like the YAML the user wrote ("steps.3.loop.max_iterations").
        """
        lines = []
        for issue in error.errors():
            parts: list[str] = []
            previous: object = None
            for part in issue["loc"]:
                if not (isinstance(previous, int) and part in union_tags):
                    parts.append(str(part))
                previous = part
            field = ".".join(parts) or whole_file_label
            message = issue["msg"].removeprefix("Value error, ")
            # Whole-file checks may report several problems joined by "; ": one line each.
            lines.extend(f"{field}: {part}" for part in message.split("; "))
        return cls(path, lines)
