"""The environment a release hands to tools that run code it did not write.

`uv build` runs a project's build backend and build requirements, `uv lock` and
`uv pip install` may build an sdist-only dependency to read it, and the smoke test runs
the installed CLI. None of them may read the index, signing or git credentials the
release shell holds (only `uv publish` gets those, see publish.py), and none may load a
checkout through PYTHONPATH or an activated VIRTUAL_ENV.

Index read settings (UV_INDEX_URL, UV_INDEX_*_PASSWORD, ...) are kept: a build may need
the org's index to fetch its build requirements.
"""

from __future__ import annotations

import os

#: Dropped by prefix: upload credentials and every release setting (the signing key file
#: and its password among them).
SECRET_PREFIXES = ("UV_PUBLISH_", "AGENTIC_RELEASE_")
#: Dropped by name: git host tokens a CI step may export, and paths that would make an
#: interpreter import something other than what was installed.
DROPPED = frozenset(
    {"GIT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
)


def scrubbed_env(**extra: str) -> dict[str, str]:
    """This process's environment without release secrets or checkout paths, plus
    `extra`. Names compare case-blind (Windows environment names are)."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(SECRET_PREFIXES) and name.upper() not in DROPPED
    }
    env.update(extra)
    return env
