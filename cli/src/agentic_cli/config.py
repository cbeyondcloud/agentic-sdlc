"""Where the CLI keeps its user-level files (M1-49).

`~/.agentic` on Windows, macOS and Linux alike (the user's home folder, from
`Path.home()`), or `AGENTIC_HOME` when set. It holds the user config
(`config.yaml`), the policy cache, personal skills and fetched tools; a repository's
own files live in its `.agentic/` folder instead.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentic.yamlio import YamlFileError, load_yaml, read_text
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

CONFIG_FILE = "config.yaml"


def agentic_home(env: Mapping[str, str] = os.environ, home: Path | None = None) -> Path:
    override = env.get("AGENTIC_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return (home or Path.home()) / ".agentic"


def user_config(home: Path) -> dict[str, Any]:
    """The user config (`<home>/config.yaml`); empty when the file does not exist."""
    path = home / CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        data, _ = load_yaml(read_text(path))
    except YamlFileError as error:
        raise CliError(
            f"{path}: {error}", ExitCode.INVALID, hint="fix the file or remove it"
        ) from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CliError(f"{path}: expected a mapping of settings", ExitCode.INVALID)
    return data
