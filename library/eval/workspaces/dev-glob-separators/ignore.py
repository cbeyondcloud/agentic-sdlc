"""Ignore rules for the file watcher (patterns use '/')."""

from fnmatch import fnmatchcase


def is_ignored(path: str, patterns: list) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)
