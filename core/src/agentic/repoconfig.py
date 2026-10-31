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
    codeintel: {adapter: auto}

`codeintel.adapter` picks the symbol-level adapter (M3-27): `auto` (the default) routes
by the repo's language - codebase-memory-mcp for a Maven or Gradle repo when the org has
pinned it, code-review-graph otherwise - and `code-review-graph` or `codebase-memory`
override that. A JVM repo also gets the Maven/Gradle test conventions, whatever the
setting. `domain-graph` routes like `auto` and adds the estate's domain graph (built by
Zyroflo's graph build) alongside: `graph` is its folder (default `.agentic/local/graph`)
and `component` this repo's component in it (unset: the repo is the whole estate).
"""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from agentic.definitions.errors import DefinitionError
from agentic.yamlio import YamlFileError, dump_yaml, load_yaml

CONFIG_REL = ".agentic/config.yaml"
DEFAULT_GRAPH = ".agentic/local/graph"  # the domain graph's folder when `graph` is not set
_SLUG = r"^[a-z][a-z0-9-]{0,63}$"


class RepoConfigError(DefinitionError):
    """`.agentic/config.yaml` is missing a setting, has an unknown one, or is not YAML."""

    kind = "repo config"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodeIntelConfig(_Strict):
    # auto: routed by language (M3-27); domain-graph: a built domain graph of the estate,
    # alongside the routed symbol adapter (M2-43)
    adapter: Literal["auto", "code-review-graph", "codebase-memory", "domain-graph", "none"] = (
        "auto"
    )
    graph: str | None = Field(default=None, min_length=1)  # repo-relative, absolute or ~
    component: str | None = Field(default=None, min_length=1)  # None: the repo is the estate

    @property
    def graph_folder(self) -> str:
        """Where the built domain graph is: `graph`, or the git-ignored local folder."""
        return self.graph or DEFAULT_GRAPH


class RepoConfig(_Strict):
    version: Literal[1] = 1
    classification: str = Field(pattern=_SLUG)
    team: str | None = Field(default=None, pattern=_SLUG)
    pipelines: list[str] = []
    components: list[str] = []  # repo-relative submodule paths (POSIX)
    codeintel: CodeIntelConfig = CodeIntelConfig()
    # M2-61: how an in-place component (Zyroflo `vcs.deny_raw_git`) is put back at a commit,
    # by component path ("." = the repository): a command with `{commit}`. The other half of
    # the commit-command override is the profile's `vcs.commit_command`; org policy's
    # `commit_override.allow` decides whether either may run
    rollback_commands: dict[str, Annotated[str, StringConstraints(strip_whitespace=True)]] = {}

    @model_validator(mode="after")
    def components_stay_inside(self) -> Self:
        for path in self.components:
            if not _inside_repo(path):
                raise ValueError(f"components entry {path!r} must be a path inside the repo")
        for path, command in self.rollback_commands.items():
            if path != "." and not _inside_repo(path):
                raise ValueError(f"rollback_commands key {path!r} must be a path inside the repo")
            if not command:
                raise ValueError(f"rollback_commands entry {path!r} names no command")
        return self


def _inside_repo(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return bool(path) and not path.startswith(("/", "\\")) and ":" not in path and ".." not in parts


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
