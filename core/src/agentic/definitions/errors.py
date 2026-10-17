"""Errors raised while loading agent and pipeline definition files."""

from pathlib import Path


class DefinitionError(Exception):
    """A definition file could not be loaded or failed validation.

    Carries the file path and one readable line per problem, so the CLI can
    show the user exactly what to fix.
    """

    def __init__(self, path: Path, errors: list[str]) -> None:
        self.path = path
        self.errors = errors
        bullet_list = "\n".join(f"  - {error}" for error in errors)
        super().__init__(f"{path}: invalid definition\n{bullet_list}")
