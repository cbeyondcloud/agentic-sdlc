"""Upload a signed release to the org's internal package index with `uv publish`.

Only to an internal index: a public one (PyPI, TestPyPI) is refused, since these are the
org's internal packages and a public upload would leak them (and invite look-alikes).
Plain http is refused except on loopback, where the tests' local index lives. Only after
the whole release folder verifies (signing.verify_dir), and only the wheels and sdists.

Credentials reach uv only through the environment it reads (UV_PUBLISH_USERNAME and
UV_PUBLISH_PASSWORD, or UV_PUBLISH_TOKEN), never the command line, and trusted
publishing is off (it is a public-index feature that would probe the CI's identity).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from agentic_release import signing

PUBLIC_HOSTS = frozenset(
    {"pypi.org", "upload.pypi.org", "test.pypi.org", "pypi.python.org", "files.pythonhosted.org"}
)
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
CREDENTIAL_VARS = ("UV_PUBLISH_USERNAME", "UV_PUBLISH_PASSWORD", "UV_PUBLISH_TOKEN")
DIST_SUFFIXES = (".whl", ".tar.gz")


class PublishError(RuntimeError):
    """The release cannot be published there."""


def check_index_url(url: str) -> None:
    """Refuse a public index, a non-http(s) URL, and plain http off loopback."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise PublishError(f"{url}: give the internal index's http(s) upload URL")
    if host in PUBLIC_HOSTS or host.endswith(".pypi.org"):
        raise PublishError(
            f"{url} is a public index: agentic-sdlc releases go only to the org's internal "
            "index (set AGENTIC_RELEASE_INDEX_URL or --url)"
        )
    if parts.scheme == "http" and host not in LOOPBACK:
        raise PublishError(f"{url}: plain http would send the index credentials in clear text")


def publish(
    folder: Path,
    url: str,
    *,
    allowed: dict,
    identity: str,
    check_url: str | None = None,
    env: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str]:
    """Verify `folder`, then upload its wheels and sdists to `url`; returns their names.

    `check_url` (the index's simple URL) makes a repeat publish skip files already there.
    `env` is laid over this process's environment; the credentials come from there."""
    check_index_url(url)
    if check_url:
        check_index_url(check_url)
    verified = signing.verify_dir(folder, allowed, identity)
    dists = [name for name in verified if name.endswith(DIST_SUFFIXES)]
    if not dists:
        raise PublishError(f"{folder}: no wheels or sdists to publish")
    uv = shutil.which("uv")
    if uv is None:
        raise PublishError("uv is not on PATH: install uv (https://docs.astral.sh/uv/)")
    cmd = [uv, "publish", "--publish-url", url, "--trusted-publishing", "never"]
    if check_url:
        cmd += ["--check-url", check_url]
    cmd += [str(folder / name) for name in dists]
    environment = {**os.environ, **(env or {})}
    done = run(cmd, env=environment, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        detail = done.stderr or done.stdout or ""
        for name in CREDENTIAL_VARS:
            secret = environment.get(name)
            if secret:
                detail = detail.replace(secret, "***")
        raise PublishError(f"uv publish failed:\n{detail.strip()}")
    return dists
