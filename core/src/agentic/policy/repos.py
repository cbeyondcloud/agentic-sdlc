"""Which repositories org policy allows (`allowed_repo_patterns`, org.yaml): one matcher for the
CLI's init guard (check 5) and the control plane's repo registration (SP15), so both answer
alike. Moved from `agentic_cli.repo_guard` with its behaviour unchanged (DF-199 included)."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable


def normalise_remote(url: str) -> str:
    """`host/owner/repo`, lower case, from an https, ssh or scp-style git URL. Only a
    URL with a scheme can carry a port; in the scp form (`host:owner/repo`) the part
    after the colon is always the path, even when the owner is a number (DF-199)."""
    text = url.strip().lower()
    scheme = re.match(r"^[a-z][a-z0-9+.-]*://", text)
    text = re.sub(r"^[^@/]*@", "", text[scheme.end() :] if scheme else text)  # user@
    if scheme:
        text = re.sub(r"^([^/:]+):\d+(?=/)", r"\1", text)  # host:port
    else:
        text = re.sub(r"^([^/:]+):", r"\1/", text, count=1)  # scp form host:owner/repo
    return text.rstrip("/").removesuffix(".git")


def repo_allowed(patterns: Iterable[str], remote: str) -> bool:
    """True when `remote` matches one of `patterns` (each normalised like a remote, `*`
    and `?` as globs). An empty list allows nothing: in org mode that is the safe reading."""
    origin = normalise_remote(remote)
    return any(fnmatch.fnmatchcase(origin, normalise_remote(p)) for p in patterns)


def repo_host(repo_key: str) -> str:
    """The host of a normalised `host/owner/repo` key."""
    return repo_key.split("/", 1)[0]
