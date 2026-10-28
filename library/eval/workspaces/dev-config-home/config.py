"""Where the CLI keeps its configuration."""

from pathlib import PurePosixPath, PureWindowsPath


def config_dir(env: dict, platform: str):
    """The folder for the tool's config on `platform` ('linux', 'darwin' or 'win32')."""
    if env.get("XDG_CONFIG_HOME"):
        return PurePosixPath(env["XDG_CONFIG_HOME"]) / "acme"
    return PurePosixPath(env["HOME"]) / ".config" / "acme"
