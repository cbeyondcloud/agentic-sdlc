"""The repo's committed settings: `.agentic/config.yaml` (spec §4.2; M1-52).

`agentic init --repo` writes it on a new branch; every developer `init` and run reads
it. It names the repo's sensitivity class (which must exist in org policy), the team
that owns the repo, the pipelines enabled for it (their agents decide which skills a
developer receives; empty = every pipeline visible to the repo), any submodules that
are components of a multi-repo run, and the code-intelligence adapter.

    version: 1
    classification: internal
    team: payments
    pipelines: [feature-delivery]
    components: [libs/shared]
    codeintel: {adapter: code-review-graph}
"""

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agentic.definitions.errors import DefinitionError
from agentic.yamlio import YamlFileError, dump_yaml, load_yaml

CONFIG_REL = ".agentic/config.yaml"
_SLUG = r"^[a-z][a-z0-9-]{0,63}$"


class RepoConfigError(DefinitionError):
    """`.agentic/config.yaml` is missing a setting, has an unknown one, or is not YAML."""

    kind = "repo config"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodeIntelConfig(_Strict):
    adapter: Literal["code-review-graph", "none"] = "code-review-graph"


class RepoConfig(_Strict):
    version: Literal[1] = 1
    classification: str = Field(pattern=_SLUG)
    team: str | None = Field(default=None, pattern=_SLUG)
    pipelines: list[str] = []
    components: list[str] = []  # repo-relative submodule paths (POSIX)
    codeintel: CodeIntelConfig = CodeIntelConfig()

    @model_validator(mode="after")
    def components_stay_inside(self) -> Self:
        for path in self.components:
            parts = path.replace("\\", "/").split("/")
            if not path or path.startswith(("/", "\\")) or ":" in path or ".." in parts:
                raise ValueError(f"components entry {path!r} must be a path inside the repo")
        return self


def load_repo_config(text: str, path: Path = Path(CONFIG_REL)) -> RepoConfig:
    """Parse and validate the file's text. Raises RepoConfigError."""
    try:
        data, lines = load_yaml(text)
    except YamlFileError as error:
        raise RepoConfigError(path, [str(error)]) from error
    if not isinstance(data, dict):
        raise RepoConfigError(path, ["the file must be a mapping of settings"])
    try:
        return RepoConfig.model_validate(data)
    except ValidationError as error:
        raise RepoConfigError.from_validation_error(path, error, "config", lines=lines) from error


def render_repo_config(config: RepoConfig) -> str:
    """The file's text (LF), with a header saying who changes it."""
    head = (
        "# Agentic settings for this repository (spec 4.2). Changed by pull request;\n"
        "# the classification must be one of the org policy's classes.\n"
    )
    return head + dump_yaml(config.model_dump(mode="json", exclude_none=True))
