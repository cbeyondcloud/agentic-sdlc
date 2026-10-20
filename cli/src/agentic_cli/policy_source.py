"""Where `init` gets the org policy from, and the policy cache in `~/.agentic` (M1-53).

Standalone (`--standalone --policy PATH`, spec §24): a local policy, no control plane and
no RBAC. PATH is a policy repo (a folder holding `policy/`, and optionally `skills/`,
`agents/`, `pipelines/`, `teams/`) or a `policy/` folder itself.

Org mode: the user config (`~/.agentic/config.yaml`) names the org and its policy repo:

    org: acme
    policy_repo: https://github.com/acme/agentic-org-policy.git   # or a local clone
    policy_cache_hours: 24                                         # optional

The policy repo is cloned (shallow) into a scratch folder first, so a guard refusal
leaves `~/.agentic` untouched; `LoadedPolicy.commit()` then moves it to the cache at
`~/.agentic/policy/<org>/repo/` with `fetched.json` (source, version, fetched and
expiry times). When the policy repo cannot be reached, an unexpired cache is used with
a warning; an expired one refuses, because policy must not go stale silently.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from agentic.policy import OrgPolicy, PolicyError, load_policy
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

DEFAULT_CACHE_HOURS = 24
_ORG = re.compile(r"[a-z][a-z0-9-]{0,63}")


@dataclass
class LoadedPolicy:
    policy: OrgPolicy
    root: Path  # the policy repo root (agents/, pipelines/, skills/, teams/ live here)
    mode: Literal["standalone", "org"]
    source: str
    org: str | None = None
    warnings: list[str] = field(default_factory=list)
    _pending: Path | None = field(default=None, repr=False)  # a fresh clone not yet cached
    _cache: Path | None = field(default=None, repr=False)
    _hours: int = DEFAULT_CACHE_HOURS

    def commit(self) -> None:
        """Org mode: put a freshly fetched policy repo into the cache (after the guard)."""
        if self._pending is None or self._cache is None:
            return
        target = self._cache / "repo"
        if target.exists():
            shutil.rmtree(target, onexc=_writable_and_retry)
        self._cache.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self._pending), str(target))
        now = datetime.now(UTC)
        stamp = {
            "source": self.source,
            "version": self.policy.version,
            "fetched_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=self._hours)).isoformat(),
        }
        (self._cache / "fetched.json").write_text(
            json.dumps(stamp, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        self.root, self._pending = target, None

    def discard(self) -> None:
        """Drop a fetched policy repo that was not committed (a refusal or an error)."""
        if self._pending is not None:
            shutil.rmtree(self._pending.parent, onexc=_writable_and_retry)
            self._pending = None


def resolve_policy(
    *, standalone: bool, policy_path: Path | None, home: Path, config: dict[str, Any]
) -> LoadedPolicy:
    """Load the policy for this init without writing anything (see `commit`)."""
    if standalone:
        path = policy_path or (Path(config["policy"]) if config.get("policy") else None)
        if path is None:
            raise CliError(
                "--standalone needs a local policy",
                ExitCode.USAGE,
                hint="pass --policy <policy repo or policy folder>",
            )
        root, folder = _layout(Path(path).expanduser())
        return LoadedPolicy(_load(folder), root, "standalone", str(path))
    if policy_path is not None:
        raise CliError("--policy is for --standalone", ExitCode.USAGE)
    return _org_policy(home, config)


def _layout(path: Path) -> tuple[Path, Path]:
    if (path / "policy").is_dir():
        return path, path / "policy"
    if path.is_dir():
        return (path.parent if path.name == "policy" else path), path
    raise CliError(f"no policy at {path}", ExitCode.INVALID, hint="pass the policy repo folder")


def _load(folder: Path) -> OrgPolicy:
    try:
        return load_policy(folder)
    except PolicyError as error:
        raise CliError(str(error), ExitCode.INVALID, hint="fix the policy files") from error


def _org_policy(home: Path, config: dict[str, Any]) -> LoadedPolicy:
    org, source = str(config.get("org", "")), str(config.get("policy_repo", ""))
    if not _ORG.fullmatch(org) or not source:
        raise CliError(
            "org mode needs `org` and `policy_repo` in the user config",
            ExitCode.INVALID,
            hint=f"set them in {home / 'config.yaml'}, or use --standalone --policy <path>",
        )
    hours = int(config.get("policy_cache_hours", DEFAULT_CACHE_HOURS))
    cache = home / "policy" / org
    scratch = Path(tempfile.mkdtemp(prefix="agentic-policy-"))
    clone = scratch / "repo"
    problem = _clone(source, clone)
    if problem is None:
        root, folder = _layout(clone)
        loaded = LoadedPolicy(_load(folder), root, "org", source, org, _hours=hours)
        loaded._pending, loaded._cache = clone, cache
        return loaded
    shutil.rmtree(scratch, ignore_errors=True)
    return _cached(cache, source, org, problem, hours)


def _cached(cache: Path, source: str, org: str, problem: str, hours: int) -> LoadedPolicy:
    stamp_file = cache / "fetched.json"
    try:
        stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
        expires = datetime.fromisoformat(stamp["expires_at"])
    except (OSError, ValueError, KeyError):
        raise CliError(
            f"cannot fetch the org policy ({problem}) and there is no cached copy",
            ExitCode.ERROR,
            hint="check the network and the policy_repo setting, then run init again",
        ) from None
    if datetime.now(UTC) > expires:
        raise CliError(
            f"cannot fetch the org policy ({problem}) and the cached copy expired at "
            f"{expires.isoformat(timespec='minutes')}",
            ExitCode.REFUSED,
            hint="reconnect to the network (or VPN) and run init again",
        )
    root, folder = _layout(cache / "repo")
    loaded = LoadedPolicy(_load(folder), root, "org", source, org, _hours=hours)
    loaded.warnings.append(
        f"using the cached org policy: the policy repo is unreachable ({problem})"
    )
    return loaded


def _clone(source: str, dest: Path) -> str | None:
    """Shallow-clone the policy repo; the problem in a few words, or None."""
    try:
        done = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "clone", "-q", "--depth", "1", source, str(dest)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return type(error).__name__
    if done.returncode != 0:
        lines = [line for line in done.stderr.strip().splitlines() if line.strip()]
        return lines[-1][:160] if lines else f"git clone exit {done.returncode}"
    return None


def _writable_and_retry(func, path, _error) -> None:
    os.chmod(path, 0o700)
    func(path)
