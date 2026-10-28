"""Path helpers for the sync tool."""


def dedupe_paths(paths: list, case_insensitive: bool = False) -> list:
    """Drop repeated paths, keeping order."""
    seen, out = set(), []
    for path in paths:
        key = path.casefold() if case_insensitive else path
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out
