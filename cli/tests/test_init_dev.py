"""Developer `agentic init` (M1-53): prerequisites, policy cache, skills, tools, index and
the readiness report. It writes nothing outside ~/.agentic and .agentic/local.

Temporary HOME/AGENTIC_HOME and git repos; the machine (which, run), the mirror and the
index builder are fakes. No fetched file is ever executed."""

import hashlib
import io
import json
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from cli_helpers import FIXTURES, ROOT, commit_file, git, link_dir, snapshot

from agentic.auth import Credentials
from agentic.policy.model import PlatformKey
from agentic_cli import dev_init
from agentic_cli.config import agentic_home
from agentic_cli.dev_init import IndexSkipped, InitDeps
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main
from sdlc_commons.tool_fetch import PLATFORMS

MIRROR = "https://artifacts.acme.example/agentic"
LINUX = "linux-x86_64"
CONFIG = "version: 1\nclassification: internal\nteam: payments\npipelines: [feature-delivery]\n"
DEV_SKILLS = {
    "superpowers/systematic-debugging",
    "superpowers/test-driven-development",
    "superpowers/verification-before-completion",
}


def tarball(member: str, data: bytes) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


GITLEAKS = tarball("gitleaks", b"fake scanner\n")
HELPER = tarball("helper", b"fake helper\n")
FILES = {
    f"{MIRROR}/gitleaks/8.0.0/gitleaks_linux_x64.tar.gz": GITLEAKS,
    f"{MIRROR}/helper/1.0.0/helper_linux_x64.tar.gz": HELPER,
}


def pins_yaml() -> str:
    def sha(blob: bytes) -> str:
        return hashlib.sha256(blob).hexdigest()

    return f"""tools:
  gitleaks:
    version: 8.0.0
    binary: gitleaks
    assets:
      {LINUX}: {{file: gitleaks/8.0.0/gitleaks_linux_x64.tar.gz, sha256: "{sha(GITLEAKS)}"}}
  helper:
    version: 1.0.0
    binary: helper
    when: on_request
    assets:
      {LINUX}: {{file: helper/1.0.0/helper_linux_x64.tar.gz, sha256: "{sha(HELPER)}"}}
"""


class Mirror:
    def __init__(self, offline: bool = False) -> None:
        self.offline, self.asked = offline, []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        if self.offline or url not in FILES:
            raise OSError("unreachable")
        return FILES[url]


class NoRing:
    def get_password(self, service, username):
        return None

    def set_password(self, service, username, password):
        raise AssertionError("init never stores credentials")


@pytest.fixture
def policy_repo(policy_repo: Path) -> Path:
    """The fixture policy plus skills (the shipped library), agents, pipelines and pins."""
    shutil.copytree(ROOT / "library" / "skills", policy_repo / "skills")
    (policy_repo / "agents").mkdir()
    shutil.copy(FIXTURES / "agents" / "developer.md", policy_repo / "agents")
    (policy_repo / "pipelines").mkdir()
    shutil.copy(FIXTURES / "pipelines" / "feature-delivery.yaml", policy_repo / "pipelines")
    (policy_repo / "policy" / "tool-pins.yaml").write_text(pins_yaml(), encoding="utf-8")
    return policy_repo


@pytest.fixture
def setup_repo(repo: Path) -> Path:
    commit_file(repo, ".agentic/config.yaml", CONFIG)
    return repo


@pytest.fixture
def deps(monkeypatch: pytest.MonkeyPatch, home: Path):
    """Test doubles for the machine, the mirror and the index, used by `agentic init`."""
    made = InitDeps(
        which=lambda name: f"/usr/bin/{name}" if name in {"git", "node", "srt", "rg"} else None,
        run=lambda argv: (0, "git version 2.47.0") if argv[0] == "git" else (0, "0.0.78"),
        system="Linux",
        kernel_release="6.8.0",
        credentials=Credentials(environ={"AGENTIC_TOKENLEAN_API_KEY": "k"}, keyring=NoRing()),
        download=Mirror(),
        platform=LINUX,
        index_builder=lambda repo, data_dir: "built (fake)",
        confirm=lambda text: pytest.fail("init asked for confirmation unexpectedly"),
    )
    monkeypatch.setattr(dev_init, "default_deps", lambda: made)
    return made


def init(repo: Path, policy_repo: Path, monkeypatch, *extra: str) -> int:
    monkeypatch.chdir(repo)
    return main(["--json", "init", "--standalone", "--policy", str(policy_repo), *extra])


def report_of(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def item(report: dict, name: str) -> dict:
    found = [c for c in report["checks"] if c["name"] == name]
    assert found, f"no check {name!r} in {[c['name'] for c in report['checks']]}"
    return found[0]


def test_init_writes_nothing_outside_agentic_home_and_local(
    setup_repo, policy_repo, deps, monkeypatch, tmp_path, home
):
    before = snapshot(tmp_path)
    status_before = git(setup_repo, "status", "--porcelain")
    init(setup_repo, policy_repo, monkeypatch)
    after = snapshot(tmp_path)
    allowed = (agentic_home(), setup_repo / ".agentic" / "local")
    changed = {path for path in after if before.get(path) != after[path]} | (
        set(before) - set(after)
    )
    outside = [p for p in changed if not any(Path(p).is_relative_to(a) for a in allowed)]
    assert changed and outside == []
    assert git(setup_repo, "status", "--porcelain") == status_before


def test_the_report_lists_every_check_and_a_fix_for_each_failure(
    setup_repo, policy_repo, deps, monkeypatch, capsys
):
    deps.which = lambda name: "/usr/bin/git" if name == "git" else None  # no sandbox tools
    deps.credentials = Credentials(environ={}, keyring=NoRing())
    assert init(setup_repo, policy_repo, monkeypatch) == ExitCode.ERROR  # not ready
    report = report_of(capsys)
    names = {c["name"] for c in report["checks"]}
    for expected in (
        "python",
        "git",
        "sandbox: node",
        "container engine",
        "credential",
        "policy",
        "secret scan",
        "skills",
        "tool: gitleaks",
        "tool: helper",
        "index",
    ):
        assert expected in names
    assert report["ready"] is False
    failing = [c for c in report["checks"] if c["status"] in ("missing", "warn")]
    assert failing and all(c["fix"] for c in failing)


def test_a_ready_init(setup_repo, policy_repo, deps, monkeypatch, capsys):
    assert init(setup_repo, policy_repo, monkeypatch) == ExitCode.OK
    report = report_of(capsys)
    assert report["ready"] is True
    assert item(report, "policy")["status"] == "ok"
    assert item(report, "index")["detail"] == "built (fake)"


def test_skills_are_synced_from_the_policy_repo(setup_repo, policy_repo, deps, monkeypatch, capsys):
    init(setup_repo, policy_repo, monkeypatch)
    skills = setup_repo / ".agentic" / "local" / "skills"
    installed = {f"{s.parent.name}/{s.name}" for s in skills.glob("*/*") if s.is_dir()}
    assert DEV_SKILLS <= installed
    assert item(report_of(capsys), "skills")["status"] == "ok"


def test_missing_skills_and_unknown_packs_are_reported(
    setup_repo, policy_repo, deps, monkeypatch, capsys
):  # DF-135, DF-145
    team = policy_repo / "policy" / "teams" / "payments.yaml"
    team.write_text(team.read_text(encoding="utf-8") + "skill_packs: [no-such-pack]\n", "utf-8")
    unvetted = "skills: [someone/unvetted-skill, superpowers/test-driven-development,"
    for path in (policy_repo / "agents" / "developer.md", policy_repo / "policy/agent-types.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "skills: [superpowers/test-driven-development," in text
        path.write_text(
            text.replace("skills: [superpowers/test-driven-development,", unvetted), "utf-8"
        )
    init(setup_repo, policy_repo, monkeypatch)
    report = report_of(capsys)
    assert "someone/unvetted-skill" in item(report, "skills: not in the catalog")["detail"]
    assert "no-such-pack" in item(report, "skills: unknown packs")["detail"]


def test_tools_are_fetched_and_audited(setup_repo, policy_repo, deps, monkeypatch, capsys):
    init(setup_repo, policy_repo, monkeypatch)
    report = report_of(capsys)
    assert item(report, "tool: gitleaks")["status"] == "ok"
    binary = agentic_home() / "tools" / "gitleaks" / "8.0.0" / LINUX / "gitleaks"
    assert binary.read_bytes() == b"fake scanner\n"
    audit = setup_repo / ".agentic" / "local" / "audit" / "init-events.jsonl"
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    fetched = [e for e in events if e["event"] == "init.tool_fetched"]
    assert fetched and fetched[0]["fields"]["tool"] == "gitleaks"
    assert any(e["event"] == "skills.installed" for e in events)


def test_a_second_init_uses_the_cache_offline(setup_repo, policy_repo, deps, monkeypatch, capsys):
    init(setup_repo, policy_repo, monkeypatch)
    capsys.readouterr()
    deps.download = Mirror(offline=True)
    assert init(setup_repo, policy_repo, monkeypatch) == ExitCode.OK
    assert "cached" in item(report_of(capsys), "tool: gitleaks")["detail"]


def test_a_tampered_mirror_file_is_refused(setup_repo, policy_repo, deps, monkeypatch, capsys):
    FILES_BACKUP = dict(FILES)
    try:
        FILES[f"{MIRROR}/gitleaks/8.0.0/gitleaks_linux_x64.tar.gz"] = tarball("gitleaks", b"evil")
        assert init(setup_repo, policy_repo, monkeypatch) == ExitCode.ERROR
    finally:
        FILES.update(FILES_BACKUP)
    tool = item(report_of(capsys), "tool: gitleaks")
    assert tool["status"] == "missing" and "checksum" in tool["detail"]
    assert not (agentic_home() / "tools" / "gitleaks" / "8.0.0" / LINUX / "gitleaks").exists()


def test_on_request_tools_wait_for_install_missing(
    setup_repo, policy_repo, deps, monkeypatch, capsys
):
    init(setup_repo, policy_repo, monkeypatch)
    helper = item(report_of(capsys), "tool: helper")
    assert helper["status"] == "info" and "--install-missing" in helper["detail"]
    assert not (agentic_home() / "tools" / "helper").exists()


def test_install_missing_shows_what_and_asks(setup_repo, policy_repo, deps, monkeypatch, capsys):
    asked: list[str] = []
    deps.confirm = lambda text: asked.append(text) or False  # declined
    monkeypatch.chdir(setup_repo)
    main(["init", "--standalone", "--policy", str(policy_repo), "--install-missing"])
    shown = capsys.readouterr()
    assert asked and "helper 1.0.0" in shown.out and "helper_linux_x64.tar.gz" in shown.out
    assert not (agentic_home() / "tools" / "helper").exists()

    deps.confirm = lambda text: True
    main(["init", "--standalone", "--policy", str(policy_repo), "--install-missing"])
    assert (agentic_home() / "tools" / "helper" / "1.0.0" / LINUX / "helper").is_file()


def test_install_missing_with_json_needs_yes(setup_repo, policy_repo, deps, monkeypatch, capsys):
    assert init(setup_repo, policy_repo, monkeypatch, "--install-missing") == ExitCode.USAGE
    assert init(setup_repo, policy_repo, monkeypatch, "--install-missing", "--yes") == ExitCode.OK
    assert (agentic_home() / "tools" / "helper" / "1.0.0" / LINUX / "helper").is_file()


def test_an_index_that_cannot_be_built_is_reported(
    setup_repo, policy_repo, deps, monkeypatch, capsys
):
    def skipped(repo, data_dir):
        raise IndexSkipped("the codeintel extra is not installed")

    deps.index_builder = skipped
    assert init(setup_repo, policy_repo, monkeypatch) == ExitCode.OK
    index = item(report_of(capsys), "index")
    assert index["status"] == "info" and "codeintel" in index["detail"]


def test_init_records_its_state_in_local(setup_repo, policy_repo, deps, monkeypatch):
    init(setup_repo, policy_repo, monkeypatch)
    state = json.loads((setup_repo / ".agentic" / "local" / "init.json").read_text("utf-8"))
    assert state["mode"] == "standalone" and state["policy_version"].startswith("sha256:")
    assert (setup_repo / ".agentic" / "local" / ".gitignore").read_text("utf-8") == "*\n"


def test_a_linked_local_folder_is_refused(setup_repo, policy_repo, deps, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link_dir(setup_repo / ".agentic" / "local", elsewhere)
    assert init(setup_repo, policy_repo, monkeypatch) == ExitCode.REFUSED
    assert list(elsewhere.iterdir()) == []


def test_core_platform_keys_are_the_fetcher_s():
    assert set(get_args(PlatformKey)) == set(PLATFORMS)


def test_developer_init_refuses_before_init_repo(repo, policy_repo, deps, monkeypatch, tmp_path):
    before = snapshot(tmp_path)
    assert init(repo, policy_repo, monkeypatch) == ExitCode.REFUSED
    assert snapshot(tmp_path) == before


# --- org mode: the policy cache in ~/.agentic ---


@pytest.fixture
def org_config(policy_repo: Path, home: Path) -> Path:
    git(policy_repo, "init", "-q", "-b", "main")
    git(policy_repo, "add", "-A")
    git(policy_repo, "commit", "-qm", "policy")
    config = agentic_home()
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        f"org: acme\npolicy_repo: {policy_repo.as_posix()}\n", encoding="utf-8"
    )
    return policy_repo


def org_init(repo: Path, monkeypatch) -> int:
    monkeypatch.chdir(repo)
    return main(["--json", "init"])


def test_org_init_caches_the_policy(setup_repo, org_config, deps, monkeypatch, capsys):
    assert org_init(setup_repo, monkeypatch) == ExitCode.OK
    cache = agentic_home() / "policy" / "acme"
    stamp = json.loads((cache / "fetched.json").read_text(encoding="utf-8"))
    assert stamp["version"].startswith("git:") and (cache / "repo" / "policy").is_dir()
    assert item(report_of(capsys), "policy")["detail"].startswith("org acme")


def test_org_init_offline_uses_an_unexpired_cache(
    setup_repo, org_config, deps, monkeypatch, capsys, tmp_path
):
    org_init(setup_repo, monkeypatch)
    capsys.readouterr()
    shutil.move(str(org_config), str(tmp_path / "moved-away"))  # the policy repo is gone
    assert org_init(setup_repo, monkeypatch) == ExitCode.OK
    assert item(report_of(capsys), "policy: cache")["status"] == "warn"


def test_org_init_offline_with_an_expired_cache_refuses(
    setup_repo, org_config, deps, monkeypatch, capsys, tmp_path
):
    org_init(setup_repo, monkeypatch)
    stamp_file = agentic_home() / "policy" / "acme" / "fetched.json"
    stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
    stamp["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    stamp_file.write_text(json.dumps(stamp), encoding="utf-8")
    shutil.move(str(org_config), str(tmp_path / "moved-away"))
    assert org_init(setup_repo, monkeypatch) == ExitCode.REFUSED
