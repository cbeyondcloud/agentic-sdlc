"""Plain git for publishing (M1-58): read a remote, push one branch.

Pushing uses the person's own git credentials (a credential helper or an SSH key), as
Zyroflo's `release.push` does: publishing a branch needs no API token, and the token is
never put in a URL or on a command line. The push is never forced, and git is told not
to prompt (`GIT_TERMINAL_PROMPT=0`), so a push inside a run fails with a message rather
than waiting for input nobody will type. Every message is scrubbed of URL credentials and
of the secrets the caller names, and git never inherits the framework's own tokens.
"""

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from agentic.hosting.base import HostingError, scrub_url_credentials
from agentic.sandbox.process import SECRET_NAME

_GIT = ("git", "-c", "core.fsmonitor=false")


def remote_url(where: Path, remote: str = "origin") -> str:
    """The fetch URL of `remote` in the repository at `where`."""
    _refuse_option(remote, "remote")
    done = _git(where, "remote", "get-url", remote, timeout=60)
    if done.returncode != 0:
        raise HostingError(f"no remote named {remote!r} in {where}")
    return done.stdout.strip()


def rev_parse(where: Path, rev: str) -> str:
    """The commit `rev` names in the repository at `where`."""
    _refuse_option(rev, "revision")
    done = _git(where, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}", timeout=60)
    if done.returncode != 0:
        raise HostingError(f"{rev!r} is not a commit in {where}")
    return done.stdout.strip()


def push_branch(
    where: Path,
    remote: str,
    branch: str,
    *,
    timeout: int = 600,
    secrets: Iterable[str] = (),
) -> None:
    """Push local branch `branch` to the same name on `remote` and track it. A remote that
    has moved on is a refusal, never an overwrite."""
    _refuse_option(remote, "remote")
    _refuse_option(branch, "branch")
    known = [s for s in secrets if s]
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    try:
        done = _git(where, "push", "-u", remote, refspec, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HostingError(
            _scrub(
                f"git push of {branch} to {remote} did not finish within {timeout}s - "
                f"whether it reached {remote} is unknown; check the remote before pushing again",
                known,
            )
        ) from None
    if done.returncode != 0:
        printed = "\n".join(p for p in (done.stderr.strip(), done.stdout.strip()) if p)
        tail = " / ".join(printed.splitlines()[-3:]) or f"exit {done.returncode}"
        raise HostingError(_scrub(f"git push of {branch} to {remote} failed: {tail}", known))


def _env() -> dict[str, str]:
    """This process's environment without the framework's own secrets (`AGENTIC_*` with a
    secret-like name): hooks, credential helpers and ssh wrappers run inside git and can
    read what it gets (DF-294). The person's own credentials - an SSH agent, a helper's
    token - stay: pushing needs them."""
    kept = {
        name: value
        for name, value in os.environ.items()
        if not (name.upper().startswith("AGENTIC_") and SECRET_NAME.search(name))
    }
    return {**kept, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


def _git(where: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
    env = _env()
    try:
        return subprocess.run(
            [*_GIT, "-C", str(where), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        raise HostingError(f"git could not be run: {type(error).__name__}") from None


def _refuse_option(value: str, what: str) -> None:
    """A name git would read as an option (or an empty one) is refused before git runs."""
    if not value or value.startswith("-") or any(ord(c) < 32 for c in value):
        raise HostingError(f"refused: {value!r} is not a usable {what} name")


def _scrub(text: str, secrets: list[str]) -> str:
    text = scrub_url_credentials(text)
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return text
