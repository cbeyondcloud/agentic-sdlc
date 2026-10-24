"""Developer `agentic init`, after the safety checks passed (M1-53, spec §8.1, §24).

In order:

  1. prerequisites (`agentic_cli.readiness`): Python, git, sandbox, container engine,
     Git Bash on Windows, the credential;
  2. policy: org mode caches the fetched policy repo in `~/.agentic/policy/<org>/`;
  3. skills (DF-135): the union of the skills of the agents in the repo's pipelines (an
     agent's type fills in what it leaves out), the team's packs and, for admin roles,
     the authoring skills - synced from the policy repo's `skills/` (standalone with no
     catalog: the framework's `library/skills`) into `.agentic/local/skills/`, read-only
     and hash-pinned; skills not in the catalog and unknown packs are reported (DF-145);
  4. tools (M1-51): every `always` pin (and `licence_scan` pins when the org scans
     licences) fetched hash-verified from the org mirror into `~/.agentic/tools/`;
     `on_request` pins only with `--install-missing`, after showing exactly what and
     asking;
  5. the local code-intelligence index in `.agentic/local/index/` (D39);
  6. the report, saved with the init state in `.agentic/local/init.json`.

Init writes only under `~/.agentic` and `<repo>/.agentic/local` (git-ignored by its own
`.gitignore`), and refuses when either is a link. Its events (`skills.*`,
`init.tool_fetched`, `init.completed`) are appended to
`.agentic/local/audit/init-events.jsonl`; forwarding them to the control plane comes
with M2-20.
"""

import json
import os
import platform as _platform
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import typer

from agentic.auth import Credentials
from agentic.definitions import DefinitionError, DefinitionSet
from agentic.policy import OrgPolicy, ResolveContext, ToolPin
from agentic.repoconfig import RepoConfig
from agentic.skills import CatalogError, SkillsError, load_catalog
from agentic.skills.catalog import CATALOG_FILE
from agentic.skills.install import sync_skills
from agentic.skills.select import pipeline_skills, select_skills, team_packs
from agentic_cli import readiness
from agentic_cli.download import download
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.policy_source import LoadedPolicy
from agentic_cli.readiness import Item, Report
from agentic_cli.repo_guard import RepoRoot
from agentic_cli.standalone import PLACEHOLDER, framework_root
from agentic_cli.workspace import Workspace
from sdlc_commons.tool_fetch import FetchResult, ToolFetcher, ToolFetchError

LOCAL_REL = Path(".agentic") / "local"
AUDIT_REL = Path("audit") / "init-events.jsonl"
#: What init writes inside `.agentic/local`: none of these may be a link.
LOCAL_WRITES = tuple(
    Path(rel) for rel in ("audit", AUDIT_REL, "skills", "index", "init.json", ".gitignore")
)


def bundled_skills() -> Path:
    """The framework's default skill library: a checkout's, or the installed wheel's."""
    return framework_root() / "library" / "skills"


class IndexSkipped(Exception):
    """The local index is not built, for a reason that is not a failure."""


def build_local_index(repo: Path, data_dir: Path) -> str:
    """Build (or catch up) the local index with code-review-graph, when installed."""
    from agentic.codeintel.index import LocalIndex, available

    if not available():
        raise IndexSkipped("the codeintel extra (code-review-graph) is not installed")
    LocalIndex(repo, data_dir, home_dir=data_dir / "home").ensure()
    return f"built in {LOCAL_REL.as_posix()}/index"


@dataclass
class InitDeps:
    """What init uses from the machine and the network (tests replace them)."""

    which: Callable[[str], str | None] = shutil.which
    run: Callable[[list[str]], tuple[int, str]] = readiness._run
    system: str | None = None
    kernel_release: str | None = None
    credentials: Credentials | None = None
    download: Callable[[str], bytes] = download
    platform: str | None = None  # None: this machine's
    index_builder: Callable[[Path, Path], str] = build_local_index
    confirm: Callable[[str], bool] = typer.confirm
    roles: tuple[str, ...] = ()


def default_deps() -> InitDeps:
    return InitDeps()


@dataclass
class InitAudit:
    """Append-only init events (one JSON object per line, LF)."""

    path: Path
    policy_version: str
    events: list[dict] = field(default_factory=list)

    def record(self, event: str, fields: dict[str, object]) -> None:
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "event": event,
            "policy_version": self.policy_version,
            "fields": fields,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # never through a link (O_NOFOLLOW where the OS has it; init refused links already)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)  # LF on Windows
        fd = os.open(self.path, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as log:
            log.write(json.dumps(entry, default=str) + "\n")
        self.events.append(entry)


def run_dev_init(
    root: RepoRoot,
    loaded: LoadedPolicy,
    home: Path,
    deps: InitDeps,
    *,
    install_missing: bool = False,
    assume_yes: bool = False,
    announce: Callable[[list[dict]], None] = lambda plan: None,
) -> Report:
    policy, config = loaded.policy, root.config or RepoConfig(classification="internal")
    local = _prepare_local(root.path, home)
    loaded.commit()
    audit = InitAudit(local / AUDIT_REL, policy.version)

    items = readiness.prerequisites(
        policy=policy,
        which=deps.which,
        run=deps.run,
        system=deps.system,
        kernel_release=deps.kernel_release,
        credentials=deps.credentials,
        tree=root.path,
    )
    items += _policy_items(loaded)
    if loaded.mode == "standalone":
        items.append(_runs_policy(root.path, home, loaded.root, loaded.source))
    items += _skills(root.path, local, loaded, config, deps.roles, audit)
    tools = _Tools(home, policy, deps, audit)
    items += tools.required()
    items += tools.on_request(install_missing, assume_yes, announce)
    items.append(_index(root.path, local, config, deps))

    report = Report(items)
    state = {
        "mode": loaded.mode,
        "org": loaded.org,
        "policy_source": loaded.source,
        "policy_version": policy.version,
        "initialised_at": datetime.now(UTC).isoformat(),
        **report.to_json(),
    }
    _replace_text(local / "init.json", json.dumps(state, indent=2) + "\n")
    audit.record("init.completed", {"ready": report.ready})
    return report


def _prepare_local(repo: Path, home: Path) -> Path:
    """`.agentic/local`, ready; refuses (writing nothing) when it, `.agentic`, the home
    folder or anything init writes inside `.agentic/local` is a link. A repo can commit
    links there (git tracks them, and the folder is not ignored until init runs)."""
    local = repo / LOCAL_REL
    for path in (repo / ".agentic", local, home, *(local / rel for rel in LOCAL_WRITES)):
        if path.is_symlink() or path.is_junction():
            raise CliError(
                f"{path} is a link; init writes only into real folders it owns",
                ExitCode.REFUSED,
                hint="remove the link and run init again",
            )
    local.mkdir(parents=True, exist_ok=True)
    ignore = local / ".gitignore"
    if not ignore.is_file() or ignore.read_text(encoding="utf-8") != "*\n":
        _replace_text(ignore, "*\n")
    home.mkdir(parents=True, exist_ok=True)
    return local


def _replace_text(path: Path, text: str) -> None:
    """Write `text` (LF) to a new file beside `path`, then rename it over `path`: a link
    planted at `path` is replaced, never followed."""
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def _policy_items(loaded: LoadedPolicy) -> list[Item]:
    policy = loaded.policy
    where = f"org {loaded.org}" if loaded.mode == "org" else f"standalone {loaded.source}"
    items = [Item("policy", "ok", f"{where} ({policy.version})")]
    for warning in loaded.warnings:
        if loaded.mode == "org":
            items.append(Item("policy: cache", "warn", warning, "reconnect and run init again"))
        else:
            fix = "restore it and run init again, or pass --policy <folder> to choose one"
            items.append(Item("policy: standalone", "warn", warning, fix))
    for note in loaded.notes:
        items.append(Item(f"policy: {loaded.mode}", "info", note))
    placeholders = sorted(name for name, tier in policy.tiers.items() if PLACEHOLDER in tier.model)
    if placeholders:
        tiers = loaded.root / "policy" / "tiers.yaml"
        items.append(
            Item(
                "policy: models",
                "missing",
                f"placeholder models in tiers {', '.join(placeholders)}",
                f"replace every '{PLACEHOLDER}' model in {tiers} with a model your gateway "
                "serves, then run init again",
            )
        )
    scan = policy.secret_scan
    detectors = ", ".join(scan.detectors) if scan.detectors else "the default detectors"
    items.append(
        Item(
            "secret scan",
            "info",
            f"{scan.action}: content and tool output are scanned with {detectors} before "
            "they leave this machine",
        )
    )
    return items


def _runs_policy(repo: Path, home: Path, policy_root: Path, source: str) -> Item:
    """Whether later commands in this repo (`agentic run`...) will use this policy, once
    init has recorded `source` for the repo in init.json (DF-274)."""
    found = Workspace.load(repo, home=home).policy_repo_with(Path(source))
    if found is not None and _same_folder(found, policy_root):
        return Item("policy: runs", "info", f"later commands use {policy_root}")
    using = f"use {found}" if found is not None else "find no policy"
    return Item(
        "policy: runs",
        "warn",
        f"later commands in this repo {using}, not {policy_root}",
        f"set AGENTIC_POLICY_REPO={policy_root} (or policy_repo: {policy_root.as_posix()} in "
        f"{home / 'config.yaml'})",
    )


def _same_folder(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


# --- skills ---


def _skills(
    repo: Path,
    local: Path,
    loaded: LoadedPolicy,
    config: RepoConfig,
    roles: tuple[str, ...],
    audit: InitAudit,
) -> list[Item]:
    source = loaded.root / "skills"
    if not (source / CATALOG_FILE).is_file() and loaded.mode == "standalone":
        source = bundled_skills()
    if not (source / CATALOG_FILE).is_file():
        fix = "add skills/catalog.yaml to the policy repo (start from the framework's library)"
        return [Item("skills", "warn", "no skill catalog found; agents run without skills", fix)]
    try:
        catalog = load_catalog(source)
    except CatalogError as error:
        return [Item("skills", "missing", str(error), "fix skills/catalog.yaml in the policy repo")]

    policy = loaded.policy
    items: list[Item] = []
    team_root = loaded.root / "teams" / config.team if config.team else None
    team = team_root if team_root is not None and team_root.is_dir() else None
    # as later runs see them (the policy's allow_repo_pipelines in every mode, DF-204)
    allow = policy.allow_repo_pipelines
    try:
        definitions = DefinitionSet.discover(
            loaded.root, team, repo / ".agentic", allow_repo_pipelines=allow
        )
    except DefinitionError as error:
        items.append(Item("skills: definitions", "warn", str(error), "fix the definition file"))
        try:  # a broken repo file must not leave the org's pipelines without skills
            definitions = DefinitionSet.discover(
                loaded.root, team, None, allow_repo_pipelines=False
            )
        except DefinitionError:
            definitions = DefinitionSet()
    wanted_pipelines = config.pipelines or definitions.pipeline_names()
    unknown = [name for name in wanted_pipelines if name not in definitions.pipelines]
    if unknown:
        items.append(
            Item(
                "skills: unknown pipelines",
                "warn",
                ", ".join(unknown),
                "fix pipelines: in .agentic/config.yaml by a pull request",
            )
        )
    known = [name for name in wanted_pipelines if name in definitions.pipelines]
    ctx = ResolveContext(team=config.team, repo_classification=config.classification)
    wanted = pipeline_skills(definitions, known, policy, ctx)
    selection = select_skills(catalog, wanted, packs=team_packs(policy, config.team), roles=roles)
    try:
        report = sync_skills(
            catalog, source, local / "skills", selection.names, record=audit.record
        )
    except SkillsError as error:
        items.insert(0, Item("skills", "missing", str(error), "re-pin or restore the catalog"))
        return items
    detail = (
        f"{len(selection.names)} in .agentic/local/skills ({len(report.installed)} new, "
        f"{len(report.restored)} restored, {len(report.removed)} removed)"
    )
    items.insert(0, Item("skills", "ok", detail))
    if selection.missing:
        items.append(
            Item(
                "skills: not in the catalog",
                "warn",
                ", ".join(selection.missing),
                "agents run without them; ask an org admin to vet and add them to the catalog",
            )
        )
    if selection.unknown_packs:
        items.append(
            Item(
                "skills: unknown packs",
                "warn",
                ", ".join(selection.unknown_packs),
                "no catalog skill belongs to these packs: fix the team's skill_packs",
            )
        )
    return items


# --- tools ---


class _Tools:
    def __init__(self, home: Path, policy: OrgPolicy, deps: InitDeps, audit: InitAudit) -> None:
        self.policy, self.deps = policy, deps
        self.fetcher: ToolFetcher | None = None
        self.problem = ""
        if policy.tool_pins:
            try:
                self.fetcher = ToolFetcher(
                    home / "tools",
                    policy.tool_mirror_url or "",
                    download=deps.download,
                    platform=deps.platform,
                    record=self._attested(audit.record),
                )
            except ToolFetchError as error:
                self.problem = str(error)

    def _attested(self, record: Callable[[str, dict], None]) -> Callable[[str, dict], None]:
        """`record`, with each `init.tool_fetched` event saying whether the org manifest
        marks the artefact signed (DF-208): on Windows that mark is the trust, no local
        signature check is made, and the audit must show it."""

        def attested(event: str, fields: dict) -> None:
            if event == "init.tool_fetched":
                pin = self.policy.tool_pins.get(str(fields.get("tool")))
                asset = pin.assets.get(fields.get("platform")) if pin is not None else None
                fields = {**fields, "signed_per_org_manifest": bool(asset and asset.signed)}
            record(event, fields)

        return attested

    def required(self) -> list[Item]:
        if not self.policy.tool_pins:
            return [Item("tools", "info", "org policy pins no tools")]
        if self.fetcher is None:
            return [Item("tools", "missing", self.problem, "fix tool_mirror_url in org policy")]
        items = []
        for name, pin in sorted(self.policy.tool_pins.items()):
            if pin.when == "always" or (pin.when == "licence_scan" and self.policy.licence_scan):
                items.append(_tool_item(name, pin, self.fetcher.fetch(name, pin), required=True))
            elif pin.when == "licence_scan":
                items.append(Item(f"tool: {name}", "info", "not needed: licence scanning is off"))
        return items

    def on_request(
        self, install: bool, assume_yes: bool, announce: Callable[[list[dict]], None]
    ) -> list[Item]:
        if self.fetcher is None:
            return []
        items, pending = [], []
        for name, pin in sorted(self.policy.tool_pins.items()):
            if pin.when != "on_request":
                continue
            on_path = self.deps.which(pin.binary)
            cached = self.fetcher.cached(name, pin)
            if on_path:
                items.append(Item(f"tool: {name}", "ok", str(on_path)))
            elif cached:
                items.append(self._off_path(name, Path(cached)))
            elif self.fetcher.platform not in pin.assets:
                detail = f"not installed; no artefact for {self.fetcher.platform}"
                items.append(Item(f"tool: {name}", "info", detail))
            else:
                pending.append((name, pin))
        if not pending:
            return items
        if not install:
            for name, pin in pending:
                detail = (
                    f"not installed; `agentic init --install-missing` can install {name} "
                    f"{pin.version} into ~/.agentic/tools"
                )
                items.append(Item(f"tool: {name}", "info", detail))
            return items
        plan, installable = [], []
        for name, pin in pending:
            try:
                url, dest = self.fetcher.planned(name, pin)
            except ToolFetchError as error:  # org policy validates pins; defence in depth
                items.append(Item(f"tool: {name}", "warn", str(error), _TOOL_FIXES["refused"]))
                continue
            installable.append((name, pin))
            plan.append({"tool": name, "version": pin.version, "url": url, "to": str(dest)})
        pending = installable
        if not pending:
            return items
        announce(plan)
        question = f"Install {len(plan)} tool(s) into {self.fetcher.cache_root}?"
        if not (assume_yes or self.deps.confirm(question)):
            for name, _ in pending:
                items.append(Item(f"tool: {name}", "info", "not installed (declined)"))
            return items
        for name, pin in pending:
            result = self.fetcher.fetch(name, pin)
            if result.ok and result.path is not None and not self.deps.which(pin.binary):
                items.append(self._off_path(name, Path(result.path)))
            else:
                items.append(_tool_item(name, pin, result, required=False))
        return items

    def _off_path(self, name: str, binary: Path) -> Item:
        """An installed on-request tool that PATH does not reach (DF-191): agentic and
        the sandbox look tools up on PATH, so the report says how to add its folder."""
        system = self.deps.system if self.deps.system is not None else _platform.system()
        detail = f"installed at {binary}, not on PATH"
        return Item(f"tool: {name}", "warn", detail, path_fix(binary.parent, system))


def path_fix(folder: Path, system: str) -> str:
    """How to put `folder` on PATH for the person's shell, then open a new terminal."""
    if system == "Windows":
        quoted = str(folder).replace("'", "''")
        return (
            "add it to your user PATH in PowerShell, then open a new terminal:\n"
            "[Environment]::SetEnvironmentVariable('Path', "
            f"[Environment]::GetEnvironmentVariable('Path', 'User') + ';{quoted}', 'User')"
        )
    shown = str(folder).replace("'", "'\\''")  # inside the single-quoted echo argument
    return (
        "add it to PATH in your shell profile, then open a new terminal "
        "(zsh on macOS: ~/.zshrc; bash: ~/.bashrc):\n"
        f"echo 'export PATH=\"{shown}:$PATH\"' >> ~/.zshrc"
    )


_TOOL_FIXES = {
    "refused": "ask the platform team: the mirror's file does not match the org pin, or the "
    "Windows build is not marked signed (or run agentic inside WSL2)",
    "failed": "check the network or VPN and the org's tool mirror, then run init again",
    "unavailable": "ask the platform team to pin a build for this platform",
}


def _tool_item(name: str, pin: ToolPin, result: FetchResult, *, required: bool) -> Item:
    if result.ok:
        return Item(f"tool: {name}", "ok", f"{result.status} {pin.version} ({result.path})")
    status = "missing" if required else "warn"
    fix = _TOOL_FIXES.get(result.status, "run init again")
    return Item(f"tool: {name}", status, f"{result.status}: {result.detail}", fix)


# --- index ---


def _index(repo: Path, local: Path, config: RepoConfig, deps: InitDeps) -> Item:
    if config.codeintel.adapter == "none":
        return Item("index", "info", "off (codeintel adapter: none in .agentic/config.yaml)")
    try:
        return Item("index", "ok", deps.index_builder(repo, local / "index"))
    except IndexSkipped as skipped:
        return Item("index", "info", f"not built: {skipped}")
    except Exception as error:  # noqa: BLE001 - an index failure must not stop init
        fix = "agents fall back to search; check the codeintel extra and run init again"
        return Item("index", "warn", f"build failed ({type(error).__name__})", fix)
