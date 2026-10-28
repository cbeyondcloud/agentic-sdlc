"""Build API URLs."""


def join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")
