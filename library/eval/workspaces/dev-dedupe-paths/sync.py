"""Path helpers for the sync tool."""


def dedupe_paths(paths: list, case_insensitive: bool = False) -> list:
    """Drop repeated paths, keeping order."""
    seen, out = set(), []
    for path in paths:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out
