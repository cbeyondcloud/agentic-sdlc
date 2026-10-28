"""Paths stored in the project manifest are always POSIX-style."""


def to_posix(path: str) -> str:
    """'src/app/main.py' stays as it is; separators become '/'."""
    path = path.replace("\\", "/")
    while "//" in path:
        path = path.replace("//", "/")
    return path
