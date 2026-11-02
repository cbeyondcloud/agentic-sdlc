"""Day-0 bootstrap (M3-05): from an installed control plane to a working org.

The ordered path is docs/runbooks/day-0-bootstrap.md. Each step reuses the framework's own
commands - `agentic policy check`, the chart's operator pod with `python -m agentic_cp
admin ...`, `agentic rbac assign`, `agentic init` and `agentic run --dry-run` - checks
before it changes anything, can be run again, and never prints a secret.

    uv run python scripts/bootstrap.py <step> --help
    policy-repo      --out DIR --repo-pattern 'git.example.com/acme/*' --team T=@acme/leads ...
    git-credentials  --tenant acme --out DIR --host github.com
    tenant           --slug acme --name Acme --admin-email A --token-file F --policy-repo URL
    people           --api URL --org acme --token-file F --assign EMAIL=ROLE@TEAM ...
    verify           --api URL --token-file F [--mail-to E] [--index-url U --cli-version V]
    smoke            --api URL --org acme --team T --invite-link L --repo-remote URL

One file for Windows, macOS and Linux (standard library only; `kubectl` for the tenant
step, the `agentic` CLI for the policy check, people and the smoke run). Exit codes: 0 done
or every check passed, 1 a check failed, 2 a step could not run, 3 bad input."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "policy-repo"
LIBRARY = ROOT / "library"
#: The chart's name: the operator Deployment is `<release>-agentic-platform-operator`.
CHART = "agentic-platform"
#: What the template's example names, replaced by the org's own.
EXAMPLE_ADMINS = "@your-org/agentic-org-admins"
EXAMPLE_TEAM = "payments"
EXAMPLE_LEADS = "@your-org/payments-leads"
EXAMPLE_SECURITY = " @your-org/agentic-security"
EXAMPLE_PATTERNS = 'allowed_repo_patterns: ["git.example.com/your-org/*"]'
TOKEN_MARKER = "token (shown once"
#: The smoke run's repository config (the release smoke's, scripts/agentic_release/smoke.py).
SMOKE_CONFIG = "version: 1\nclassification: internal\npipelines: [feature-delivery]\n"
#: A stand-in gateway key: a dry run calls no model and no gateway.
SMOKE_KEY = "bootstrap-smoke-no-gateway"


class BootstrapError(Exception):
    """A step could not run or was refused; `code` is the exit status."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def say(message: str) -> None:
    print(f"bootstrap: {message}", flush=True)


# ── secrets on disk ───────────────────────────────────────────────────────────


def write_secret(path: Path, value: str, *, runner: Runner | None = None) -> None:
    """Write `value` to a new file only this user may read (never overwrite one). POSIX:
    mode 0600. Windows ignores the mode, so the file's inherited ACL is replaced by one
    granting only its owner (icacls); if that fails the step says so."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise BootstrapError(
            f"{path} exists: move it away first (it may hold a token)", 3
        ) from None
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(value.strip() + "\n")
    if os.name == "nt":
        # *S-1-3-4 is OWNER RIGHTS: the file's owner (who just made it) and nobody else
        grant = ["icacls", str(path), "/inheritance:r", "/grant:r", "*S-1-3-4:F"]
        if (runner or run)(grant).returncode != 0:
            say(f"warning: could not limit {path} to its owner (icacls): limit it by hand")


def read_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise BootstrapError(f"cannot read {path}: {error.strerror}", 3) from None
    if not value:
        raise BootstrapError(f"{path} is empty", 3)
    return value


def token_from(output: str) -> tuple[str | None, str]:
    """The token after `admin tenant create`'s marker line, and the output without it."""
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(TOKEN_MARKER) and index + 1 < len(lines):
            shown = [*lines[:index], "token: written to the token file", *lines[index + 2 :]]
            return lines[index + 1].strip(), "\n".join(shown)
    return None, output


# ── commands ──────────────────────────────────────────────────────────────────


def split_command(text: str) -> list[str]:
    """A command line given as one option (`uv run agentic`), on any OS."""
    parts = shlex.split(text, posix=os.name != "nt")
    return [part.strip('"') for part in parts]


def agentic_command(given: str | None) -> list[str]:
    """The `agentic` CLI: `--agentic`, else the one on PATH, else this checkout's."""
    if given:
        return split_command(given)
    found = shutil.which("agentic")
    if found:
        return [found]
    return [sys.executable, "-c", "from agentic_cli.main import run; run()"]


Runner = Callable[..., subprocess.CompletedProcess]


def run(
    cmd: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(cmd),
            env=None if env is None else dict(env),
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        raise BootstrapError(f"{cmd[0]} did not finish within 15 minutes") from None
    except OSError as error:
        raise BootstrapError(f"{cmd[0]} could not be started ({error.strerror})") from None


def _last_line(done: subprocess.CompletedProcess) -> str:
    lines = [line for line in (done.stderr or done.stdout or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"exit {done.returncode}"


# ── 1. the policy repo ────────────────────────────────────────────────────────


def _pairs(values: Sequence[str], what: str, sep: str = "=") -> dict[str, str]:
    found = {}
    for value in values:
        key, given, rest = value.partition(sep)
        if not given or not key.strip() or not rest.strip():
            raise BootstrapError(f"{what}: {value!r} is not KEY{sep}VALUE", 3)
        found[key.strip()] = rest.strip()
    return found


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise BootstrapError(f"{path.name}: the template changed ({old!r} not found)")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def set_tier_values(text: str, key: str, values: Mapping[str, str]) -> str:
    """`tiers.yaml` with each named tier's `key:` (model or family) set; a tier the file
    does not have is refused."""
    lines, tier, seen = text.splitlines(keepends=True), None, set()
    for index, line in enumerate(lines):
        if found := re.match(r"^  ([a-z0-9-]+):", line):
            tier = found.group(1)
        elif tier in values and (found := re.match(rf"^(\s+){key}: \S+(.*)$", line)):
            lines[index] = f"{found.group(1)}{key}: {values[tier]}{found.group(2)}\n"
            seen.add(tier)
    if missing := sorted(set(values) - seen):
        raise BootstrapError(f"tiers.yaml has no tier {', '.join(missing)}", 3)
    return "".join(lines)


def _teams(out: Path, teams: Mapping[str, str]) -> None:
    """Replace the template's example team with the org's teams (CODEOWNERS included)."""
    codeowners = out / "CODEOWNERS"
    example_lines = (
        f"/teams/{EXAMPLE_TEAM}/                 {EXAMPLE_LEADS}\n",
        f"/policy/teams/{EXAMPLE_TEAM}.yaml      {EXAMPLE_LEADS} {EXAMPLE_ADMINS}\n",
    )
    text = codeowners.read_text(encoding="utf-8")
    if not all(line in text for line in example_lines):
        raise BootstrapError("CODEOWNERS: the template's example team lines changed")
    text = text.replace(example_lines[0], "").replace(example_lines[1], "")
    for team, leads in teams.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", team):
            raise BootstrapError(f"--team: {team!r} is not a team name (a-z, 0-9, -)", 3)
        if team != EXAMPLE_TEAM:
            shutil.copy2(
                out / "policy" / "teams" / f"{EXAMPLE_TEAM}.yaml",
                out / "policy" / "teams" / f"{team}.yaml",
            )
            shutil.copytree(out / "teams" / EXAMPLE_TEAM, out / "teams" / team)
            for overlay in (out / "teams" / team / "pipelines" / "overlays").glob("*.yaml"):
                overlay.unlink()  # the example overlay names the example team's repo
        text += f"/teams/{team}/ {leads}\n/policy/teams/{team}.yaml {leads} {EXAMPLE_ADMINS}\n"
    if EXAMPLE_TEAM not in teams:
        (out / "policy" / "teams" / f"{EXAMPLE_TEAM}.yaml").unlink()
        shutil.rmtree(out / "teams" / EXAMPLE_TEAM)
    codeowners.write_text(text, encoding="utf-8", newline="\n")


def _git(repo: Path, *args: str, runner: Runner = run) -> None:
    done = runner(["git", "-c", "core.autocrlf=false", *args], cwd=repo)
    if done.returncode != 0:
        raise BootstrapError(f"git {args[0]}: {_last_line(done)}")


def build_policy_repo(args: argparse.Namespace, *, runner: Runner = run) -> Path:
    """The template plus the framework library, the org's names filled in, committed on
    `main` with `staging` and `dev` branches. Returns the repository folder. A failure
    leaves no half-made folder behind, so the step can simply be run again."""
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise BootstrapError(f"{out} is not empty: the policy repo is made in a new folder", 3)
    try:
        return _build_policy_repo(args, out, runner)
    except BaseException:
        shutil.rmtree(out, onexc=_writable_and_retry)
        raise


def _writable_and_retry(func, path, _error) -> None:
    os.chmod(path, 0o700)  # git makes object files read-only (Windows needs them writable)
    func(path)


def _build_policy_repo(args: argparse.Namespace, out: Path, runner: Runner) -> Path:
    teams = _pairs(args.team, "--team")
    models = _pairs(args.model, "--model")
    families = _pairs(args.family, "--family")
    # never a checkout's byte-code (tests import the budget guard from the template)
    shutil.copytree(
        TEMPLATE, out, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    # the framework's library, which the template does not duplicate (its README, step 1)
    for agent in sorted((LIBRARY / "agents").glob("*.md")):
        shutil.copy2(agent, out / "agents" / agent.name)
    for pipeline in sorted((LIBRARY / "pipelines").glob("*.yaml")):
        shutil.copy2(pipeline, out / "pipelines" / pipeline.name)
    shutil.rmtree(out / "skills")
    shutil.copytree(LIBRARY / "skills", out / "skills")
    if teams:
        _teams(out, teams)
    if args.admins:
        _edit(out / "CODEOWNERS", EXAMPLE_ADMINS, " ".join(args.admins))
    # never leave the example security team in CODEOWNERS: GitHub flags an unknown owner
    _edit(out / "CODEOWNERS", EXAMPLE_SECURITY, "".join(f" {s}" for s in args.security))
    if not args.security:
        say("CODEOWNERS: no security reviewers on /policy/, /skills/ and CODEOWNERS (--security)")
    if args.repo_pattern:
        _edit(
            out / "policy" / "org.yaml",
            EXAMPLE_PATTERNS,
            f"allowed_repo_patterns: {json.dumps(args.repo_pattern)}",
        )
    if args.sandbox_required_for is not None:
        names = [n for n in args.sandbox_required_for.split(",") if n.strip() and n != "none"]
        _edit(
            out / "policy" / "org.yaml",
            "required_for: [team, regulated]",
            f"required_for: [{', '.join(n.strip() for n in names)}]",
        )
    tiers = out / "policy" / "tiers.yaml"
    text = set_tier_values(tiers.read_text(encoding="utf-8"), "model", models)
    tiers.write_text(set_tier_values(text, "family", families), encoding="utf-8", newline="\n")
    if args.cli_version:
        _edit(
            out / ".github" / "workflows" / "policy-check.yml",
            'AGENTIC_VERSION: "0.1.0"',
            f'AGENTIC_VERSION: "{args.cli_version}"',
        )
    who = ["-c", "user.name=agentic bootstrap", "-c", "user.email=bootstrap@agentic.invalid"]
    _git(out, "init", "-q", "-b", "main", runner=runner)
    _git(out, "add", "-A", runner=runner)
    message = "policy: day-0 bootstrap from the template"
    _git(out, *who, "commit", "-q", "-m", message, runner=runner)
    for branch in ("staging", "dev"):  # one long-lived branch per environment (M3-04)
        _git(out, "branch", branch, runner=runner)
    return out


def cmd_policy_repo(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if (out / ".git").is_dir():
        say(f"{out} is already a git repository: not rebuilt (delete it to start again)")
    else:
        build_policy_repo(args)
        say(f"policy repo made in {out} (branches main, staging, dev)")
    if args.no_check:
        say("policy check skipped (--no-check)")
        return 0
    done = run([*agentic_command(args.agentic), "policy", "check", str(out)])
    print(done.stdout, end="")
    if done.returncode != 0:
        print(done.stderr, end="", file=sys.stderr)
        say("the policy repo does not pass the gate: fix the problems above")
        return 1
    say("next: push main, staging and dev to your git host and protect them (runbook step 1)")
    return 0


# ── 2. git credentials for the control plane ─────────────────────────────────


def gitconfig_text(slug: str, host: str, username: str, ca: bool) -> str:
    """A tenant's GIT_CONFIG_GLOBAL: a credential helper that reads the token file mounted
    beside it (`<slug>.token`), so the token itself is never in this file."""
    token = f"/etc/agentic/git/{slug}.token"
    lines = [
        f"# The control plane's git access for tenant {slug} (M3-05). Mounted with {slug}.token",
        "# (a GitHub App installation token or a read-only token) from the chart's",
        "# gitConfigSecret at /etc/agentic/git.",
        f'[credential "https://{host}"]',
        "\thelper =",
        f'\thelper = "!f() {{ test \\"$1\\" = get || exit 0; echo username={username}; '
        f"printf 'password='; cat {token}; }}; f\"",
    ]
    if ca:
        lines += [f'[http "https://{host}/"]', f"\tsslCAInfo = /etc/agentic/git/{slug}-ca.pem"]
    return "\n".join(lines) + "\n"


def cmd_git_credentials(args: argparse.Namespace) -> int:
    slug = args.tenant
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
        raise BootstrapError(f"--tenant: {slug!r} is not a tenant slug", 3)
    if not re.fullmatch(r"[A-Za-z0-9.-]+(:[0-9]+)?", args.host):
        raise BootstrapError(f"--host: {args.host!r} is not a host name", 3)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.username):  # it goes into a shell snippet
        raise BootstrapError(f"--username: {args.username!r} is not a user name", 3)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    config = out / f"{slug}.gitconfig"
    config.write_text(
        gitconfig_text(slug, args.host, args.username, bool(args.ca_file)),
        encoding="utf-8",
        newline="\n",
    )
    files = [f"--from-file={slug}.gitconfig={config}"]
    if args.ca_file:
        ca = out / f"{slug}-ca.pem"
        shutil.copy2(args.ca_file, ca)
        files.append(f"--from-file={slug}-ca.pem={ca}")
    say(f"wrote {config}")
    say("put the token in a file only you can read, then create the Secret (no token on the")
    say("command line) and name it in the chart's gitConfigSecret:")
    print(
        f"  kubectl -n {args.namespace} create secret generic {args.secret} "
        + " ".join(files)
        + f" --from-file={slug}.token=<the token file>"
    )
    say("a GitHub App's tokens expire hourly: let External Secrets write <slug>.token instead")
    return 0


# ── 3. the tenant and its first org admin ────────────────────────────────────


def operator_name(release: str) -> str:
    """The chart's operator Deployment (`agentic.fullname` + `-operator`)."""
    base = release if CHART in release else f"{release}-{CHART}"
    return f"{base[:63].rstrip('-')}-operator"


@dataclass
class Kube:
    kubectl: list[str]
    namespace: str
    deployment: str
    runner: Runner = run

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        return self.runner([*self.kubectl, "-n", self.namespace, *args])

    def checked(self, *args: str) -> str:
        done = self(*args)
        if done.returncode != 0:
            raise BootstrapError(f"kubectl {args[0]}: {_last_line(done)}")
        return done.stdout

    def operator(self, *argv: str) -> subprocess.CompletedProcess:
        """`python -m agentic_cp <argv>` in the operator pod."""
        return self("exec", f"deploy/{self.deployment}", "--", "python", "-m", "agentic_cp", *argv)

    def scale(self, replicas: int) -> None:
        self.checked("scale", f"deploy/{self.deployment}", f"--replicas={replicas}")
        if replicas:
            self.checked("rollout", "status", f"deploy/{self.deployment}", "--timeout=300s")


def _tenant_slugs(listing: str) -> set[str]:
    return {line.split("\t", 1)[0] for line in listing.splitlines() if line.strip()}


def cmd_tenant(args: argparse.Namespace, *, runner: Runner = run) -> int:
    kubectl = split_command(args.kubectl)
    if args.kubeconfig:
        kubectl += ["--kubeconfig", args.kubeconfig]
    kube = Kube(kubectl, args.namespace, args.operator or operator_name(args.release), runner)
    token_file = Path(args.token_file)
    say(f"scaling up the operator pod {kube.deployment}")
    try:
        kube.scale(1)  # inside: a rollout that times out still ends scaled down
        listing = kube.operator("admin", "tenant", "list")
        if listing.returncode != 0:
            raise BootstrapError(f"admin tenant list: {_last_line(listing)}")
        if args.slug in _tenant_slugs(listing.stdout):
            say(f"tenant {args.slug} exists: not created again")
            if args.recover_token:
                if token_file.exists():
                    raise BootstrapError(f"{token_file} exists: move it away first", 3)
                argv = ["admin", "issue-token", "--tenant", args.slug, "--email", args.admin_email]
                _keep_token(kube.operator(*argv), token_file, "admin issue-token")
                say(f"a recovery token for {args.admin_email} is in {token_file}")
            else:
                say("a lost first-admin token: run again with --recover-token")
        else:
            if token_file.exists():
                raise BootstrapError(f"{token_file} exists: move it away first", 3)
            argv = ["admin", "tenant", "create", "--slug", args.slug, "--name", args.name]
            argv += ["--admin-email", args.admin_email]
            if args.gateway_tenant:
                argv += ["--gateway-tenant", args.gateway_tenant]
            _keep_token(kube.operator(*argv), token_file, "admin tenant create")
            say(f"the first org admin's token is in {token_file} (only you can read it)")
        if args.policy_repo:
            argv = ["admin", "tenant", "set-policy-repo", "--tenant", args.slug]
            source = kube.operator(*argv, "--repo", args.policy_repo)
            if source.returncode != 0:
                raise BootstrapError(f"admin tenant set-policy-repo: {_last_line(source)}")
            print(f"  | {source.stdout.strip()}")
        print(f"  | {kube.operator('admin', 'tenant', 'list').stdout.strip()}")
    finally:
        if not args.keep_operator:
            try:
                kube.scale(0)
                say("operator pod scaled back to zero")
            except BootstrapError as error:  # never hide the step's own error
                say(f"warning: the operator pod may still run ({error}): scale it to zero")
    return 0


def _keep_token(done: subprocess.CompletedProcess, token_file: Path, what: str) -> None:
    """The token an operator command printed, into an owner-only file; its other output
    is shown. An error names the exit status and stderr only: stdout may hold the token."""
    token, shown = token_from(done.stdout)
    if done.returncode != 0 or token is None:
        reason = _last_line(
            subprocess.CompletedProcess(done.args, done.returncode, "", done.stderr)
        )
        raise BootstrapError(f"{what}: {reason}")
    try:
        write_secret(token_file, token)
    except OSError as error:
        raise BootstrapError(
            f"{token_file} could not be written ({error.strerror}); the token is lost: run "
            "again with --recover-token and a writable --token-file"
        ) from None
    for line in shown.splitlines():
        print(f"  | {line}")


# ── 4. people and roles ───────────────────────────────────────────────────────


def _cli_env(home: Path, url: str, token: str, org: str) -> dict[str, str]:
    """The CLI's environment for a step: its own AGENTIC_HOME in org mode, the token from
    a variable (never the keyring of this machine), no AGENTIC_* setting from outside."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("AGENTIC_")}
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"org: {org}\ncontrol_plane_url: {url}\n", encoding="utf-8", newline="\n"
    )
    env.update(
        AGENTIC_HOME=str(home),
        AGENTIC_CONTROL_PLANE_URL=url,
        AGENTIC_CONTROL_PLANE_TOKEN=token,
    )
    return env


def parse_assignment(value: str) -> tuple[str, str, str | None]:
    """`email=role` or `email=role@team`."""
    email, given, rest = value.partition("=")
    role, _, team = rest.partition("@")
    if not given or "@" not in email or not role:
        raise BootstrapError(f"--assign: {value!r} is not EMAIL=ROLE or EMAIL=ROLE@TEAM", 3)
    return email.strip(), role.strip(), team.strip() or None


def cmd_people(args: argparse.Namespace, *, runner: Runner = run) -> int:
    token = read_secret(Path(args.token_file))
    wanted = [parse_assignment(value) for value in args.assign]
    failed = 0
    with tempfile.TemporaryDirectory(prefix="agentic-bootstrap-") as work:
        env = _cli_env(Path(work) / "home", args.api, token, args.org)
        agentic = agentic_command(args.agentic)
        for email, role, team in wanted:
            argv = [*agentic, "--json", "rbac", "assign", email, role]
            done = runner([*argv, *(["--team", team] if team else [])], env=env)
            where = f"team {team}" if team else "the org"
            if done.returncode != 0:
                reason = _last_line(done)
                if "assignment already exists" in reason:  # the API's 409
                    say(f"{email} already holds {role} in {where}")
                    continue
                say(f"{email}: {role} in {where} refused: {reason}")
                failed += 1
                continue
            try:
                made = json.loads(done.stdout or "{}")
            except ValueError:
                made = {}
            note = made.get("invite_note")
            invited = "invited" if made.get("invited") and not note else note or "known already"
            say(f"{email} holds {role} in {where}: {invited}")
    return 1 if failed else 0


# ── 5. verify ─────────────────────────────────────────────────────────────────


@dataclass
class Http:
    base: str
    token: str | None = None
    context: ssl.SSLContext | None = None
    calls: list[str] = field(default_factory=list)

    def get(self, path: str, *, auth: bool = True) -> tuple[int, str]:
        url = path if path.startswith(("http://", "https://")) else self.base.rstrip("/") + path
        self.calls.append(url)
        request = urllib.request.Request(url, headers={"Accept": "application/json, text/html"})
        if auth and self.token:  # never forwarded to a redirect's host
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=30, context=self.context) as reply:
                return reply.status, reply.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise BootstrapError(f"{url} cannot be reached ({reason})") from None

    def json(self, path: str, *, auth: bool = True):
        status, body = self.get(path, auth=auth)
        try:
            return status, json.loads(body) if body else None
        except ValueError:
            return status, None


@dataclass
class Check:
    name: str
    status: str  # PASS, FAIL, MANUAL
    detail: str


def _check(name: str, ok: bool, good: str, bad: str) -> Check:
    return Check(name, "PASS" if ok else "FAIL", good if ok else bad)


def verify_checks(http: Http, args: argparse.Namespace) -> list[Check]:
    checks: list[Check] = []
    status, ready = http.json("/readyz", auth=False)
    checks.append(
        _check(
            "control plane ready",
            status == 200 and bool(ready) and ready.get("db_enforced") is True,
            "/readyz: ready, database guarantees enforced",
            f"/readyz answered {status}: {ready} (helm test; the API's log)",
        )
    )
    status, me = http.json("/v1/me")
    roles = {a.get("role") for a in (me or {}).get("assignments", [])} if status == 200 else set()
    checks.append(
        _check(
            "first org admin",
            "org-admin" in roles,
            f"{(me or {}).get('user', {}).get('email')} is org-admin of "
            f"{(me or {}).get('tenant', {}).get('slug')}",
            f"/v1/me answered {status}, roles {sorted(r for r in roles if r)}: check the token",
        )
    )
    status, active = http.json("/v1/policy/active")
    if status == 200 and active:
        detail = f"{active['version']} on {active['branch']}"
        checks.append(Check("policy repo synced", "PASS", detail))
    else:
        _, versions = http.json("/v1/policy/versions")
        latest = (versions or [{}])[0] if isinstance(versions, list) else {}
        why = "; ".join(latest.get("problems", [])[:3]) or "nothing fetched yet (the worker log)"
        checks.append(Check("policy repo synced", "FAIL", f"no active policy: {why}"))
    status, assignments = http.json("/v1/rbac/assignments")
    people = assignments if status == 200 and isinstance(assignments, list) else []
    checks.append(
        _check(
            "roles assigned",
            len(people) >= 1 + len(args.mail_to),
            f"{len(people)} assignments",
            f"{len(people)} assignments (status {status}): run the people step",
        )
    )
    if args.mail_to:
        status, mails = http.json("/v1/notifications?limit=500")
        mails = mails if status == 200 and isinstance(mails, list) else []
        for email in args.mail_to:
            mine = [m for m in mails if m.get("recipient") == email and m.get("kind") == "invite"]
            state = mine[0] if mine else {}
            checks.append(
                _check(
                    f"invite mailed to {email}",
                    state.get("status") == "sent",
                    "sent through the SMTP relay",
                    f"{state.get('status', 'no invite')}: {state.get('last_error') or ''} "
                    "(smtp.* values, publicUrl; `agentic rbac mail`)",
                )
            )
    if args.index_url:
        if not args.cli_version:
            raise BootstrapError("--index-url needs --cli-version", 3)
        index = Http(args.index_url, context=http.context)
        for package, wheel in (("agentic-cli", "agentic_cli"), ("agentic-core", "agentic_core")):
            status, page = index.get(f"{args.index_url.rstrip('/')}/{package}/", auth=False)
            name = f"{wheel}-{args.cli_version}-py3-none-any.whl"
            checks.append(
                _check(
                    f"{package} {args.cli_version} on the index",
                    status == 200 and name in page,
                    name,
                    f"{name} not on {args.index_url} (status {status}): scripts/release.py publish",
                )
            )
    checks += [
        Check("branch protection", "MANUAL", "main, staging, dev: code owners, `check` + `budget`"),
        Check("git host access", "MANUAL", "the GitHub App (or token) reaches the policy repo"),
        Check(
            "Jira", "MANUAL", "per developer: `jira:` in config.yaml, `agentic login --for jira`"
        ),
    ]
    return checks


def _context(ca_file: str | None) -> ssl.SSLContext | None:
    return ssl.create_default_context(cafile=ca_file) if ca_file else None


def cmd_verify(args: argparse.Namespace) -> int:
    http = Http(args.api, read_secret(Path(args.token_file)), _context(args.ca_file))
    checks = verify_checks(http, args)
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"  {check.status:<6} {check.name:<{width}}  {check.detail}")
    failed = [c for c in checks if c.status == "FAIL"]
    say(
        f"{len(failed)} failed, {sum(c.status == 'PASS' for c in checks)} passed, "
        f"{sum(c.status == 'MANUAL' for c in checks)} to confirm by hand"
    )
    return 1 if failed else 0


# ── 6. the smoke run ──────────────────────────────────────────────────────────


def redeem_invite(http: Http, link: str) -> str:
    """The control-plane token an invite link gives (`#` fragment), redeemed once."""
    token = link.partition("#")[2].strip()
    if not token:
        raise BootstrapError("--invite-link: the link has no #token part", 3)
    request = urllib.request.Request(
        http.base.rstrip("/") + "/v1/invites/redeem",
        data=json.dumps({"token": token}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=http.context) as reply:
            return json.loads(reply.read())["control_plane_token"]
    except urllib.error.HTTPError as error:
        why = "used, expired or unknown" if error.code in (404, 410) else "see the API's log"
        raise BootstrapError(f"the invite was refused ({error.code}): {why}", 1) from None
    except (urllib.error.URLError, OSError) as error:
        raise BootstrapError(f"{http.base} cannot be reached ({error})") from None


def cmd_smoke(args: argparse.Namespace, *, runner: Runner = run) -> int:
    http = Http(args.api, context=_context(args.ca_file))
    if args.invite_link:
        token = redeem_invite(http, args.invite_link)
        say("invite redeemed: the developer's token is kept in memory only")
    elif args.token_file:
        token = read_secret(Path(args.token_file))
    else:
        raise BootstrapError("give --invite-link or --token-file", 3)
    agentic = agentic_command(args.agentic)
    with tempfile.TemporaryDirectory(prefix="agentic-smoke-") as work:
        home, repo = Path(work) / "home", Path(work) / "repo"
        env = _cli_env(home, args.api, token, args.org)
        env["AGENTIC_TOKENLEAN_API_KEY"] = SMOKE_KEY
        if args.ca_file:
            env["SSL_CERT_FILE"] = str(Path(args.ca_file).resolve())
        with (home / "config.yaml").open("a", encoding="utf-8", newline="\n") as config:
            config.write(f"team: {args.team}\n")
        (repo / ".agentic").mkdir(parents=True)
        (repo / ".agentic" / "config.yaml").write_text(SMOKE_CONFIG, encoding="utf-8", newline="\n")
        who = ["-c", "user.name=agentic smoke", "-c", "user.email=smoke@agentic.invalid"]
        _git(repo, "init", "-q", "-b", "main", runner=runner)
        _git(repo, "remote", "add", "origin", args.repo_remote, runner=runner)
        _git(repo, "add", "-A", runner=runner)
        _git(repo, *who, "commit", "-q", "-m", "agentic: repo config", runner=runner)
        steps = [
            ("init", ["--json", "init"]),
            (
                "dry run",
                ["--json", "run", "feature-delivery", "--dry-run", "--input", "request=smoke"],
            ),
        ]
        for name, argv in steps:
            done = runner([*agentic, *argv], env=env, cwd=repo)
            if done.returncode != 0:
                say(f"smoke: `agentic {' '.join(argv[1:3])}` exited {done.returncode}")
                print(done.stdout[-3000:], end="")
                print(done.stderr[-3000:], end="", file=sys.stderr)
                return 1
            say(f"smoke: {name} passed")
            if name == "dry run":
                plan = json.loads(done.stdout or "{}")
                org = plan.get("org") or {}
                say(f"smoke: the control plane's answer: {json.dumps(org, sort_keys=True)[:400]}")
    say("smoke run passed: the CLI, the org policy and the control plane agree (no model called)")
    return 0


# ── the command line ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    steps = parser.add_subparsers(dest="step", required=True)

    repo = steps.add_parser("policy-repo", help="1. the policy repo from the template")
    repo.add_argument("--out", required=True, help="a new folder for the repository")
    repo.add_argument("--repo-pattern", action="append", default=[], help="host/owner/* (repeat)")
    repo.add_argument(
        "--admins",
        action="append",
        required=True,
        help="the org admins' CODEOWNERS handle (@org/team or @login; the budget guard's admins)",
    )
    repo.add_argument(
        "--security",
        action="append",
        default=[],
        help="security reviewers' CODEOWNERS handle on policy/, skills/ and CODEOWNERS (repeat)",
    )
    repo.add_argument("--team", action="append", default=[], help="TEAM=@org/leads (repeat)")
    repo.add_argument("--model", action="append", default=[], help="TIER=provider/model")
    repo.add_argument("--family", action="append", default=[], help="TIER=model-family")
    repo.add_argument("--cli-version", help="the release the policy-check workflow installs")
    repo.add_argument(
        "--sandbox-required-for",
        help="override org.yaml sandbox required_for (comma list, or none); default: as shipped",
    )
    repo.add_argument("--agentic", help="the agentic command (default: on PATH, or this checkout)")
    repo.add_argument("--no-check", action="store_true", help="skip `agentic policy check`")
    repo.set_defaults(run=cmd_policy_repo)

    git = steps.add_parser("git-credentials", help="2. a tenant's gitconfig for the chart")
    git.add_argument("--tenant", required=True)
    git.add_argument("--out", required=True)
    git.add_argument("--host", default="github.com")
    git.add_argument("--username", default="x-access-token", help="GitHub: x-access-token")
    git.add_argument("--ca-file", help="the git host's CA certificate (a private CA)")
    git.add_argument("--namespace", default="agentic")
    git.add_argument("--secret", default="agentic-git", help="the chart's gitConfigSecret")
    git.set_defaults(run=cmd_git_credentials)

    tenant = steps.add_parser("tenant", help="3. the tenant and its first org admin")
    tenant.add_argument("--slug", required=True)
    tenant.add_argument("--name", required=True)
    tenant.add_argument("--admin-email", required=True)
    tenant.add_argument("--token-file", required=True, help="a new file for the admin's token")
    tenant.add_argument("--policy-repo", help="the policy repo URL the control plane fetches")
    tenant.add_argument("--gateway-tenant", help="the TokenLean tenant")
    tenant.add_argument("--namespace", default="agentic")
    tenant.add_argument("--release", default="agentic")
    tenant.add_argument("--operator", help="the operator Deployment (default: from --release)")
    tenant.add_argument("--kubectl", default="kubectl")
    tenant.add_argument("--kubeconfig")
    tenant.add_argument("--keep-operator", action="store_true", help="leave it scaled up")
    tenant.add_argument(
        "--recover-token",
        action="store_true",
        help="the tenant exists: write a recovery token for --admin-email (admin issue-token)",
    )
    tenant.set_defaults(run=cmd_tenant)

    people = steps.add_parser("people", help="4. roles for the first people (invites)")
    people.add_argument("--api", required=True, help="the control plane's URL")
    people.add_argument("--org", required=True, help="the tenant's slug")
    people.add_argument("--token-file", required=True, help="the org admin's token file")
    people.add_argument("--assign", action="append", required=True, help="EMAIL=ROLE[@TEAM]")
    people.add_argument("--agentic")
    people.set_defaults(run=cmd_people)

    verify = steps.add_parser("verify", help="5. the day-0 checklist")
    verify.add_argument("--api", required=True)
    verify.add_argument("--token-file", required=True)
    verify.add_argument("--mail-to", action="append", default=[], help="an invited address")
    verify.add_argument("--index-url", help="the internal index's simple URL")
    verify.add_argument("--cli-version")
    verify.add_argument("--ca-file", help="a private CA for the control plane or index")
    verify.set_defaults(run=cmd_verify)

    smoke = steps.add_parser("smoke", help="6. a developer's first session, no model called")
    smoke.add_argument("--api", required=True)
    smoke.add_argument("--org", required=True)
    smoke.add_argument("--team", required=True)
    smoke.add_argument(
        "--invite-link",
        help="an invite link (redeemed here, so it no longer works: invite a smoke account)",
    )
    smoke.add_argument("--token-file", help="or a developer's token file")
    smoke.add_argument(
        "--repo-remote",
        required=True,
        help="a remote URL inside the policy's allowed_repo_patterns (nothing is pushed)",
    )
    smoke.add_argument("--agentic")
    smoke.add_argument("--ca-file")
    smoke.set_defaults(run=cmd_smoke)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except BootstrapError as error:
        print(f"bootstrap: error: {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    sys.exit(main())
