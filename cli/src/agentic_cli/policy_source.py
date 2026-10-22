"""Where `init` gets the org policy from, and the policy cache in `~/.agentic` (M1-53).

Standalone (`--standalone --policy PATH`, spec §24): a local policy, no control plane and
no RBAC. PATH is a policy repo (a folder holding `policy/`, and optionally `skills/`,
`agents/`, `pipelines/`, `teams/`) or a `policy/` folder itself.

Standalone with no PATH (and no local `policy:` or, without `org:`, `policy_repo:` in
the user config: `config.local_policy_setting`) keeps the policy an earlier
`init --standalone --policy` recorded for this repository, saying so; when that folder
is gone it says so too (a warning) and falls back to the framework's
template (M1-62): `~/.agentic/policy/standalone/` when it exists - the person's own copy,
never overwritten - else a fresh one assembled in a scratch folder
(`agentic_cli.standalone`) that `LoadedPolicy.commit()` installs there after the guard.

Org mode: the user config (`~/.agentic/config.yaml`) names the org and its policy repo:

    org: acme
    policy_repo: https://github.com/acme/agentic-org-policy.git   # or a local clone
    policy_cache_hours: 24                                         # optional

The policy repo is cloned (shallow) into a scratch folder first, so a guard refusal
leaves `~/.agentic` untouched; `LoadedPolicy.commit()` then moves it to the cache at
`~/.agentic/policy/<org>/repo/` with `fetched.json` (source, version, fetched and
expiry times). When the policy repo cannot be reached, an unexpired cache is used with
a warning; an expired one refuses, because policy must not go stale silently.

Org mode with a control plane (`control_plane_url`, SP15 ND-2, ruling R19): the policy is
the tenant's validated bundle instead of a clone - fetched from the control plane,
refused unless its digest matches, extracted safely (`tarfile` `filter="data"`) and
checked against the tree digest the control plane computed, then loaded under the
version the control plane validated (`git:<commit>`). `fetched.json` then also records
`source_kind: control-plane`, the URL, the tenant (to refuse a cache of another tenant
after a re-login), the digests and the version. Unreachable, the unexpired cache of the
same tenant is used with a warning. `Workspace.policy()` re-checks the cached tree's
digest, so a locally edited org policy is refused.
"""

import gzip
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from agentic.orgapi import Me
from agentic.policy import OrgPolicy, PolicyError, load_policy, tree_digest
from agentic.policy.portable import portable_path_problems
from agentic_cli import control_plane
from agentic_cli.config import local_policy_setting
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.standalone import STANDALONE_DIR, assemble

DEFAULT_CACHE_HOURS = 24
_ORG = re.compile(r"[a-z][a-z0-9-]{0,63}")
#: Waits before each retry of a rename an antivirus scanner holds up (about 3 s in all).
_RENAME_WAITS = (0.1, 0.2, 0.4, 0.8, 1.6)
#: Swap leftovers older than this are an interrupted init's, never a running one's (DF-377).
_LEFTOVER_SECONDS = 600
CONTROL_PLANE_SOURCE = "control-plane"


@dataclass
class LoadedPolicy:
    policy: OrgPolicy
    root: Path  # the policy repo root (agents/, pipelines/, skills/, teams/ live here)
    mode: Literal["standalone", "org"]
    source: str
    org: str | None = None
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # for the person to know (report: info)
    _pending: Path | None = field(default=None, repr=False)  # a fresh clone not yet cached
    _cache: Path | None = field(default=None, repr=False)
    _install: Path | None = field(default=None, repr=False)  # standalone template target
    _hours: int = DEFAULT_CACHE_HOURS
    me: Me | None = None  # who the control plane says the user is (org mode with one)
    stamp: dict = field(default_factory=dict)  # extra fetched.json fields (control plane)

    def commit(self) -> None:
        """Put a freshly fetched (org) or assembled (standalone template) policy repo in
        its place under `~/.agentic`; called only after the guard passed."""
        if self._pending is not None and self._install is not None:
            if self._install.exists():  # made meanwhile (another init): theirs is kept
                self.discard()
                root, folder = _layout(self._install)
                self.policy, self.root = _load(folder), root
                return
            if not _install_atomically(self._pending, self._install):
                self.discard()  # another init installed it meanwhile: theirs is kept
                root, folder = _layout(self._install)
                self.policy, self.root = _load(folder), root
                return
            shutil.rmtree(self._pending.parent, ignore_errors=True)  # the scratch folder
            self.root, self._pending = self._install, None
            return
        if self._pending is None or self._cache is None:
            return
        target = self._cache / "repo"
        self._cache.mkdir(parents=True, exist_ok=True)
        try:
            _swap_in(self._pending, target)
        finally:
            self.discard()  # the scratch folder, in every case (DF-201)
        now = datetime.now(UTC)
        stamp = {
            "source": self.source,
            "version": self.policy.version,
            "fetched_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=self._hours)).isoformat(),
            **self.stamp,
        }
        staged = self._cache / f".fetched.json.tmp-{os.getpid()}"
        staged.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(staged, self._cache / "fetched.json")  # the stamp only after the swap
        self.root = target

    def discard(self) -> None:
        """Drop a fetched policy repo that was not committed (a refusal or an error)."""
        if self._pending is not None:
            shutil.rmtree(self._pending.parent, onexc=_writable_and_retry)
            self._pending = None


def resolve_policy(
    *,
    standalone: bool,
    policy_path: Path | None,
    home: Path,
    config: dict[str, Any],
    recorded: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> LoadedPolicy:
    """Load the policy for this init without writing anything (see `commit`).
    `recorded` is the policy repo an earlier standalone init recorded for this
    repository (`workspace.recorded_root`), kept when no policy is given."""
    if standalone:
        setting = local_policy_setting(config)
        path = policy_path or (Path(setting) if setting else None)
        if path is None and recorded is not None and recorded != home / STANDALONE_DIR:
            return _recorded_policy(recorded, home)
        if path is None:
            return _standalone_template(home)
        path = Path(path).expanduser().absolute()  # init.json records it for later commands
        root, folder = _layout(path)
        return LoadedPolicy(_load(folder), root, "standalone", str(path))
    if policy_path is not None:
        raise CliError("--policy is for --standalone", ExitCode.USAGE)
    return _org_policy(home, config, os.environ if env is None else env)


def _recorded_policy(recorded: Path, home: Path) -> LoadedPolicy:
    """The policy this repository was initialised with, kept (never the template in
    its place, silently); when its folder is gone, the template with a warning."""
    if (recorded / "policy").is_dir():
        loaded = LoadedPolicy(_load(recorded / "policy"), recorded, "standalone", str(recorded))
        loaded.notes.append(
            f"kept the policy recorded for this repository ({recorded}); pass --policy "
            "to choose another"
        )
        return loaded
    loaded = _standalone_template(home)
    loaded.warnings.append(
        f"the policy recorded for this repository ({recorded}) is gone: init uses the "
        f"standalone template ({loaded.source}) instead"
    )
    return loaded


def _standalone_template(home: Path) -> LoadedPolicy:
    """The person's standalone policy repo, or the framework template ready to install."""
    target = home / STANDALONE_DIR
    if target.is_dir():
        root, folder = _layout(target)
        return LoadedPolicy(_load(folder), root, "standalone", str(target))
    scratch = Path(tempfile.mkdtemp(prefix="agentic-standalone-"))
    try:
        built = assemble(scratch / "repo")
        loaded = LoadedPolicy(_load(built / "policy"), built, "standalone", str(target))
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    loaded._pending, loaded._install = built, target
    loaded.notes.append(
        f"installed the framework's standalone template into {target}; it is your copy: "
        "edit it there (init never overwrites it)"
    )
    return loaded


def _layout(path: Path) -> tuple[Path, Path]:
    if (path / "policy").is_dir():
        return path, path / "policy"
    if path.is_dir():
        return (path.parent if path.name == "policy" else path), path
    raise CliError(f"no policy at {path}", ExitCode.INVALID, hint="pass the policy repo folder")


def _load(folder: Path, version: str | None = None) -> OrgPolicy:
    try:
        return load_policy(folder, version=version)
    except PolicyError as error:
        raise CliError(str(error), ExitCode.INVALID, hint="fix the policy files") from error


def _org_policy(home: Path, config: dict[str, Any], env: Mapping[str, str]) -> LoadedPolicy:
    org, source = str(config.get("org", "")), str(config.get("policy_repo", ""))
    url = control_plane.control_plane_url(env, config)
    if _ORG.fullmatch(org) and url:
        return _control_plane_policy(home, config, env, org, url)
    if not _ORG.fullmatch(org) or not source:
        raise CliError(
            "org mode needs `org` and `policy_repo` in the user config",
            ExitCode.INVALID,
            hint=f"set them in {home / 'config.yaml'}, or use --standalone --policy <path>",
        )
    hours = _cache_hours(config.get("policy_cache_hours", DEFAULT_CACHE_HOURS), home)
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


def _control_plane_policy(
    home: Path, config: dict[str, Any], env: Mapping[str, str], org: str, url: str
) -> LoadedPolicy:
    """The tenant's validated bundle from the control plane (ND-2), ready to cache."""
    hours = _cache_hours(config.get("policy_cache_hours", DEFAULT_CACHE_HOURS), home)
    cache = home / "policy" / org
    client = control_plane.client_for(env, config, home)  # refused when not signed in
    assert client is not None  # a URL is configured
    try:
        with client:
            me = client.me()
            active = client.active_policy()
            data = client.bundle(active)  # digest checked
    except control_plane.ControlPlaneUnavailable as error:
        return _cached_from_control_plane(cache, client.base_url, org, str(error), hours, home)
    scratch = Path(tempfile.mkdtemp(prefix="agentic-policy-"))
    repo = scratch / "repo"
    try:
        _extract(data, repo)
        if tree_digest(repo) != active.tree_digest:
            raise CliError(
                "the policy bundle does not extract to the tree the control plane validated",
                ExitCode.REFUSED,
                hint="run `agentic init` again; if it repeats, tell your org admin",
            )
        policy = _load(repo / "policy", version=active.version)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    loaded = LoadedPolicy(policy, repo, "org", client.base_url, org, _hours=hours, me=me)
    loaded._pending, loaded._cache = repo, cache
    loaded.stamp = {
        "source_kind": CONTROL_PLANE_SOURCE,
        "url": client.base_url,
        "tenant": me.tenant.slug,
        "digest": active.digest,
        "tree_digest": active.tree_digest,
    }
    return loaded


def _extract(data: bytes, target: Path) -> None:
    """Unpack a policy bundle; an entry that would land outside `target`, a link or a
    device is refused (`filter="data"`)."""
    target.mkdir(parents=True)
    hint = "tell your org admin: the control plane served a bundle laptops refuse"
    member = None
    try:
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r:") as tar:
            members = tar.getmembers()
            names = [m.name.rstrip("/") for m in members if m.isfile() or m.isdir()]
            problems = portable_path_problems(n for n in names if n not in ("", "."))
            if problems:  # review F4: a name this or another laptop would mangle
                raise CliError(
                    f"the policy bundle holds a file laptops cannot extract as it is: "
                    f"{problems[0]}",
                    ExitCode.REFUSED,
                    hint=hint,
                )
            for member in members:
                tar.extract(member, target, filter="data")
    except (tarfile.TarError, OSError, EOFError, ValueError) as error:
        where = f" {member.name!r}" if member is not None else ""
        raise CliError(
            f"the policy bundle holds an unsafe or unreadable entry{where} "
            f"({type(error).__name__})",
            ExitCode.REFUSED,
            hint=hint,
        ) from None


def control_plane_stamp(root: Path) -> dict | None:
    """The `fetched.json` of a control-plane cache whose repo folder is `root`, or None."""
    if root.name != "repo":
        return None
    try:
        stamp = json.loads((root.parent / "fetched.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stamp, dict) or stamp.get("source_kind") != CONTROL_PLANE_SOURCE:
        return None
    return stamp


def check_control_plane_cache(root: Path, stamp: dict, home: Path) -> None:
    """Refuse a cached control-plane policy that was edited locally (Review Focus 3) or
    belongs to another tenant than the one the user is signed in to."""
    if tree_digest(root) != stamp.get("tree_digest"):
        raise CliError(
            "the cached org policy was changed locally",
            ExitCode.REFUSED,
            hint="run `agentic init`",
        )
    record = control_plane.login_record(home) or {}
    signed_in = record.get("tenant")
    if signed_in and stamp.get("tenant") != signed_in:
        raise CliError(
            f"the cached org policy belongs to tenant {stamp.get('tenant')!r}, but you are "
            f"signed in to {signed_in!r}",
            ExitCode.REFUSED,
            hint="run `agentic init` to fetch your tenant's policy",
        )


def _cached_from_control_plane(
    cache: Path, url: str, org: str, problem: str, hours: int, home: Path
) -> LoadedPolicy:
    """The unexpired control-plane cache of the signed-in tenant, with a warning."""
    root = cache / "repo"
    stamp = control_plane_stamp(root)
    if stamp is None:
        raise CliError(
            f"cannot fetch the org policy ({problem}) and there is no cached copy",
            ExitCode.ERROR,
            hint="check the network (or VPN), then run `agentic init` again",
        )
    try:
        expires = datetime.fromisoformat(stamp["expires_at"])
    except (KeyError, TypeError, ValueError):
        expires = datetime.min.replace(tzinfo=UTC)
    if datetime.now(UTC) > expires:
        raise CliError(
            f"cannot fetch the org policy ({problem}) and the cached copy expired at "
            f"{expires.isoformat(timespec='minutes')}",
            ExitCode.REFUSED,
            hint="reconnect to the network (or VPN) and run init again",
        )
    check_control_plane_cache(root, stamp, home)
    loaded = LoadedPolicy(
        _load(root / "policy", version=stamp.get("version")), root, "org", url, org, _hours=hours
    )
    loaded.warnings.append(
        f"using the cached org policy: the control plane is unreachable ({problem})"
    )
    return loaded


def _cache_hours(value: Any, home: Path) -> int:
    """`policy_cache_hours`: a whole number of hours, at least 1 (DF-201)."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    raise CliError(
        f"policy_cache_hours must be a whole number of hours (1 or more), not {value!r}",
        ExitCode.INVALID,
        hint=f"fix it in {home / 'config.yaml'} (the default is {DEFAULT_CACHE_HOURS})",
    )


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


def _install_atomically(source: Path, target: Path) -> bool:
    """Copy `source` beside `target` (same folder, so same volume), then rename it into
    place: `target` is either absent or whole, never half-copied (DF-281). False when
    `target` appeared meanwhile (another init); nothing of ours is then left behind."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    if staged.exists():  # left by a killed init with this pid
        shutil.rmtree(staged, onexc=_writable_and_retry)
    try:
        shutil.copytree(source, staged)
        _replace_retrying(staged, target)
    except OSError as error:
        shutil.rmtree(staged, ignore_errors=True)
        if target.is_dir():
            return False
        if isinstance(error, PermissionError):
            raise CliError(
                f"cannot install the standalone policy into {target}: {error}",
                ExitCode.ERROR,
                hint="another program (often an antivirus scan) is holding the new files; "
                "wait a moment, then run `agentic init` again",
            ) from error
        raise
    return True


def _swap_in(source: Path, target: Path) -> None:
    """Replace the folder `target` with a copy of `source`, by renames only (DF-201):
    the copy is staged beside `target` (same volume), the old folder is moved aside,
    the copy renamed in, the old one removed. A failure part-way puts the old folder
    back, so `target` is always whole; when another init swapped its own fresh copy in
    meanwhile, that one is kept."""
    staged = target.with_name(f".{target.name}.new-{os.getpid()}")
    old = target.with_name(f".{target.name}.old-{os.getpid()}")
    _remove_leftovers(target)
    for leftover in (staged, old):  # left by a killed init with this pid
        if leftover.exists():
            shutil.rmtree(leftover, onexc=_writable_and_retry)
    try:
        shutil.copytree(source, staged)
        if target.exists():
            _replace_retrying(target, old)
        try:
            _replace_retrying(staged, target)
        except OSError:
            if target.exists():  # another init's fresh copy: kept
                return
            if old.exists():
                os.replace(old, target)
            raise
    except OSError as error:
        raise CliError(
            f"cannot update the org policy cache in {target.parent}: {error}",
            ExitCode.ERROR,
            hint="another program (often an antivirus scan) is holding the files; the "
            "previous cache is kept - wait a moment, then run `agentic init` again",
        ) from error
    finally:
        for leftover in (staged, old):
            try:  # git makes its pack files read-only: Windows needs them writable first
                shutil.rmtree(leftover, onexc=_writable_and_retry)
            except OSError:
                pass  # removed on the next init (the leftover check above)


def _remove_leftovers(target: Path) -> None:
    """Remove what interrupted inits left beside the cache (DF-377): `.repo.new-*`,
    `.repo.old-*` and `.fetched.json.tmp-*` older than ten minutes (a younger one may be a
    running init's). Errors are ignored: the next init tries again."""
    folder = target.parent
    patterns = (f".{target.name}.new-*", f".{target.name}.old-*", ".fetched.json.tmp-*")
    cutoff = time.time() - _LEFTOVER_SECONDS
    for pattern in patterns:
        for leftover in folder.glob(pattern):
            try:
                if leftover.lstat().st_mtime > cutoff:
                    continue
                if leftover.is_dir():
                    shutil.rmtree(leftover, onexc=_writable_and_retry)
                else:
                    leftover.unlink()
            except OSError:
                pass


def _replace_retrying(staged: Path, target: Path) -> None:
    """`os.replace`, retried for a few seconds on access denied: on Windows an
    antivirus scanner opens files just copied, and a folder cannot be renamed while
    a file in it is open (shutil.move hid this by copying again)."""
    for wait in _RENAME_WAITS:
        try:
            os.replace(staged, target)
            return
        except PermissionError:
            if target.exists():  # another init won the race: not a held handle
                raise
            time.sleep(wait)
    os.replace(staged, target)


def _writable_and_retry(func, path, _error) -> None:
    os.chmod(path, 0o700)
    func(path)
