"""`agentic init --standalone` with no `--policy` (M1-62): the framework's template is
installed once into ~/.agentic/policy/standalone/, kept when edited, found by later
commands, and the report says what to edit and, on Windows, how to get WSL2.

Temporary HOME/AGENTIC_HOME and git repos; the machine (which, run) and the index
builder are fakes."""

import json
from pathlib import Path

import pytest
from cli_helpers import commit_file

from agentic.auth import Credentials
from agentic.sandbox.readiness import Readiness
from agentic_cli import dev_init, runtime
from agentic_cli.config import agentic_home
from agentic_cli.dev_init import InitDeps
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main
from agentic_cli.standalone import STANDALONE_DIR
from agentic_cli.workspace import Workspace

CONFIG = "version: 1\nclassification: internal\npipelines: [feature-delivery]\n"
USER = "dev@example.invalid"


class NoRing:
    def get_password(self, service, username):
        return None

    def set_password(self, service, username, password):
        raise AssertionError("init never stores credentials")


@pytest.fixture
def setup_repo(repo: Path) -> Path:
    commit_file(repo, ".agentic/config.yaml", CONFIG)
    return repo


@pytest.fixture
def deps(monkeypatch: pytest.MonkeyPatch, home: Path) -> InitDeps:
    made = InitDeps(
        which=lambda name: f"/usr/bin/{name}" if name in {"git", "node", "srt", "rg"} else None,
        run=lambda argv: (0, "git version 2.47.0") if argv[0] == "git" else (0, "0.0.78"),
        system="Linux",
        kernel_release="6.8.0",
        credentials=Credentials(environ={"AGENTIC_TOKENLEAN_API_KEY": "k"}, keyring=NoRing()),
        download=lambda url: pytest.fail("the template pins no tools"),
        platform="linux-x86_64",
        index_builder=lambda repo, data_dir: "built (fake)",
        confirm=lambda text: pytest.fail("init asked for confirmation unexpectedly"),
    )
    monkeypatch.setattr(dev_init, "default_deps", lambda: made)
    for name in ("AGENTIC_POLICY_REPO", "AGENTIC_FRAMEWORK_ROOT", "AGENTIC_TEAM"):
        monkeypatch.delenv(name, raising=False)
    return made


def init(repo: Path, monkeypatch, capsys, *extra: str) -> tuple[int, dict]:
    monkeypatch.chdir(repo)
    code = main(["--json", "init", "--standalone", *extra])
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else {})


def item(report: dict, name: str) -> dict | None:
    found = [c for c in report["checks"] if c["name"] == name]
    return found[0] if found else None


def installed() -> Path:
    return agentic_home() / STANDALONE_DIR


def edit_models() -> None:
    tiers = installed() / "policy" / "tiers.yaml"
    text = tiers.read_text(encoding="utf-8")
    text = text.replace("change-me/", "acme/").replace("local/change-me", "local/acme-small")
    tiers.write_text(text, encoding="utf-8")


def test_init_installs_the_template_and_asks_for_real_models(setup_repo, deps, monkeypatch, capsys):
    code, report = init(setup_repo, monkeypatch, capsys)
    assert code == ExitCode.ERROR  # not ready: the models are placeholders
    root = installed()
    for rel in (
        "policy/tiers.yaml",
        "agents/developer.md",
        "pipelines/feature-delivery.yaml",
        "skills/catalog.yaml",
        "templates/agents/qa.md",
        "README.md",
    ):
        assert (root / rel).is_file(), rel
    models = item(report, "policy: models")
    assert models["status"] == "missing"
    assert str(root / "policy" / "tiers.yaml") in models["fix"]
    assert item(report, "skills")["status"] == "ok"  # synced from the installed catalog
    assert "installed" in item(report, "policy: standalone")["detail"]


def test_an_edited_copy_is_kept_and_init_is_then_ready(setup_repo, deps, monkeypatch, capsys):
    init(setup_repo, monkeypatch, capsys)
    edit_models()
    edited = (installed() / "policy" / "tiers.yaml").read_text(encoding="utf-8")
    code, report = init(setup_repo, monkeypatch, capsys)
    assert code == ExitCode.OK, report
    assert item(report, "policy: models") is None
    assert (installed() / "policy" / "tiers.yaml").read_text(encoding="utf-8") == edited
    assert item(report, "policy: standalone") is None  # nothing new was installed
    runs = item(report, "policy: runs")
    assert runs["status"] == "info" and str(installed()) in runs["detail"]


def test_a_guard_refusal_installs_nothing(setup_repo, deps, monkeypatch, capsys):
    (setup_repo / "sub").mkdir()
    code, _ = init(setup_repo / "sub", monkeypatch, capsys)  # not the repo root
    assert code == ExitCode.REFUSED
    assert not installed().exists()


def test_later_commands_find_the_installed_policy(setup_repo, deps, monkeypatch, capsys):
    init(setup_repo, monkeypatch, capsys)
    edit_models()
    assert Workspace.load(setup_repo, env={}, home=agentic_home()).policy_repo == installed()

    monkeypatch.setenv("AGENTIC_USER", USER)
    monkeypatch.setenv("AGENTIC_TRACES", "off")
    monkeypatch.setattr(runtime, "machine_readiness", lambda: Readiness("macos", ()))
    code = main(["--json", "run", "feature-delivery", "--input", "request=x", "--dry-run"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["pipeline"] == "feature-delivery" and plan["regime"] == "team"
    assert plan["steps"][0]["model"] == "acme/balanced-model"  # from the edited copy
    # The only problem left is the publish action, which arrives with M1-58.
    problems = [(p["step"], p["check"]) for p in plan["preflight"]["problems"]]
    assert problems == [("publish", "action")], plan["preflight"]
    assert code == ExitCode.INVALID


def test_a_policy_later_commands_will_not_find_is_reported(
    setup_repo, deps, policy_repo, monkeypatch, capsys
):
    _, report = init(setup_repo, monkeypatch, capsys, "--policy", str(policy_repo))
    runs = item(report, "policy: runs")
    assert runs["status"] == "warn" and "AGENTIC_POLICY_REPO" in runs["fix"]


def test_windows_without_wsl2_is_told_exactly_how_to_install_it(
    setup_repo, deps, monkeypatch, capsys
):
    deps.system, deps.kernel_release = "Windows", "10.0.26100"
    deps.which = lambda name: "C:/Program Files/Git/cmd/git.exe" if name == "git" else None
    _, report = init(setup_repo, monkeypatch, capsys)
    sandbox = [c for c in report["checks"] if c["name"].startswith("sandbox")]
    assert sandbox and all(c["status"] == "warn" for c in sandbox)  # solo still runs
    assert any("wsl --install" in c["fix"] for c in sandbox)
    assert any("team, regulated runs refuse without it" in c["detail"] for c in sandbox)
