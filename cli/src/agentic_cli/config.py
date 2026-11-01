"""Where the CLI keeps its user-level files (M1-49).

`~/.agentic` on Windows, macOS and Linux alike (the user's home folder, from
`Path.home()`), or `AGENTIC_HOME` when set. It holds the user config
(`config.yaml`), the policy cache, personal skills and fetched tools; a repository's
own files live in its `.agentic/` folder instead.
"""

import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
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


def local_policy_setting(config: Mapping[str, Any]) -> str | None:
    """The local policy folder the user config names: `policy`, or `policy_repo` when no
    `org` is set. With an org, `policy_repo` is where init fetches the org policy from
    (often a URL); later commands read init's cache instead (DF-274)."""
    for key in ("policy",) if config.get("org") else ("policy", "policy_repo"):
        value = str(config.get(key) or "").strip()
        if value:
            return value
    return None


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
    except OSError as error:  # permission denied, or held by another program (DF-252)
        raise CliError(
            f"{path} cannot be read: {error.strerror or error}",
            ExitCode.INVALID,
            hint="check the file's permissions (it must be readable by you), or remove it",
        ) from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CliError(f"{path}: expected a mapping of settings", ExitCode.INVALID)
    return data


@dataclass(frozen=True)
class ConfigChange:
    """What `set_user_config` did: the keys it set (each with the value it replaced, None
    when it was added), or why it wrote nothing (`reason`)."""

    path: Path
    written: bool
    changed: dict[str, str | None] = field(default_factory=dict)
    reason: str | None = None


def set_user_config(home: Path, values: Mapping[str, str]) -> ConfigChange:
    """Set top-level string settings in the user config (DF-952), editing the file as text
    so every other line - comments, order, quoting, line endings, a byte-order mark - stays
    as written. A key already set to another value is replaced in place, the old line kept
    above it as a comment. The new text must parse back to the old settings plus these, and
    replaces the file in one step (a temporary file, then a rename), keeping its mode. A
    file that is not a YAML mapping, or whose value spans several lines, is left alone:
    `reason` says why, and the caller shows the lines instead."""
    path = home / CONFIG_FILE
    if path.is_symlink():  # replacing it would detach a dotfile manager's link
        return ConfigChange(path, False, reason="it is a symbolic link; edit the file it points to")
    try:
        raw = path.read_bytes() if path.is_file() else b""
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        return ConfigChange(path, False, reason=f"it cannot be read ({error})")
    bom = raw.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if "\r\n" in text else "\n"
    try:
        data, lines = load_yaml(text)
    except YamlFileError as error:
        return ConfigChange(path, False, reason=f"it is not valid YAML ({error})")
    data = {} if data is None else data
    if not isinstance(data, dict):
        return ConfigChange(path, False, reason="it is not a mapping of settings")
    rows = text.splitlines()
    changed: dict[str, str | None] = {}
    added: list[str] = []
    for key, value in values.items():
        if key not in data:
            added.append(f"{key}: {_scalar(value)}")
            changed[key] = None
            continue
        if data[key] == value:
            continue
        at = (lines.get((key,)) or 0) - 1
        if not isinstance(data[key], str | None) or not _one_line(rows, at):
            return ConfigChange(path, False, reason=f"its {key} is not a one-line value")
        rows[at : at + 1] = [
            f"# {rows[at].rstrip()}  (replaced by agentic)",
            f"{key}: {_scalar(value)}",
        ]
        changed[key] = data[key]
    if not changed:
        return ConfigChange(path, False)
    new = newline.join(rows + added) + newline
    try:  # never write what reads back otherwise (a '...' end marker, a flow mapping)
        same = load_yaml(new)[0] == {**data, **values}
    except YamlFileError:
        same = False
    if not same:
        return ConfigChange(path, False, reason="the edited file would not read back the same")
    try:
        _replace(path, ("\ufeff" if bom else "") + new)
    except OSError as error:
        return ConfigChange(path, False, reason=f"it cannot be written ({error})")
    return ConfigChange(path, True, changed)


def _one_line(rows: list[str], at: int) -> bool:
    """Whether the top-level key on row `at` has its whole value on that row: no indented
    continuation follows before the next key."""
    if not 0 <= at < len(rows) or rows[at][:1].isspace():
        return False
    for row in rows[at + 1 :]:
        if not row.strip() or row.lstrip().startswith("#"):
            continue
        return not row[:1].isspace()
    return True


def _scalar(value: str) -> str:
    """`value` as a YAML scalar: plain when it reads back unchanged, else double-quoted."""
    try:
        plain = load_yaml(f"k: {value}\n")[0] == {"k": value}
    except YamlFileError:
        plain = False
    return value if plain else json.dumps(value)


def _replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(text.encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
