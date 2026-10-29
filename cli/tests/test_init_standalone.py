"""`agentic init --standalone` with no `--policy` (M1-62): the framework's template is
installed once into ~/.agentic/policy/standalone/, kept when edited, found by later
commands, and the report says what to edit and, on Windows, how to get WSL2.

Temporary HOME/AGENTIC_HOME and git repos; the machine (which, run) and the index
builder are fakes."""

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from cli_helpers import commit_file

from agentic.auth import Credentials
from agentic.sandbox.readiness import Readiness
from agentic_cli import __version__, dev_init, runtime
from agentic_cli.config import agentic_home
from agentic_cli.dev_init import InitDeps
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main
from agentic_cli.standalone import STAMP, STANDALONE_DIR, framework_root, template_digest
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


def test_not_ready_names_what_to_fix_not_a_machine_failure(setup_repo, deps, monkeypatch, capsys):
    """With only placeholder models to fix, the message says so (DF-284)."""
    monkeypatch.chdir(setup_repo)
    code = main(["--json", "init", "--standalone"])
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert code == ExitCode.ERROR
    assert "policy: models" in error["error"] and "machine" not in error["error"]


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


def test_the_installed_copy_records_the_template_it_came_from(
    setup_repo, deps, monkeypatch, capsys
):
    """DF-273: so a later CLI can tell the person their copy is from an older template."""
    init(setup_repo, monkeypatch, capsys)
    stamp = json.loads((installed() / STAMP).read_text(encoding="utf-8"))
    assert stamp == {"agentic": __version__, "template": template_digest(framework_root())}


def _newer_framework(tmp_path: Path) -> Path:
    """A copy of the framework's template and library with one agent changed: what an
    upgraded CLI bundles."""
    root = tmp_path / "framework"
    for rel in ("templates/standalone", "library/agents", "library/pipelines", "library/skills"):
        shutil.copytree(framework_root() / rel, root / rel)
    agent = root / "library" / "agents" / "developer.md"
    agent.write_text(agent.read_text(encoding="utf-8") + "\nA newer instruction.\n", "utf-8")
    return root


def test_a_newer_template_is_announced_and_the_copy_kept(
    setup_repo, deps, monkeypatch, capsys, tmp_path
):
    init(setup_repo, monkeypatch, capsys)
    edit_models()
    edited = (installed() / "policy" / "tiers.yaml").read_text(encoding="utf-8")
    monkeypatch.setenv("AGENTIC_FRAMEWORK_ROOT", str(_newer_framework(tmp_path)))
    code, report = init(setup_repo, monkeypatch, capsys)
    assert code == ExitCode.OK, report
    news = item(report, "policy: standalone")
    assert news["status"] == "info"
    assert f"agentic {__version__}" in news["detail"] and str(installed()) in news["detail"]
    assert "agentic init --standalone --refresh-template" in news["detail"]
    assert (installed() / "policy" / "tiers.yaml").read_text(encoding="utf-8") == edited
    assert "A newer instruction" not in (installed() / "agents" / "developer.md").read_text(
        encoding="utf-8"
    )


def test_a_copy_installed_before_stamps_is_told_too(setup_repo, deps, monkeypatch, capsys):
    init(setup_repo, monkeypatch, capsys)
    edit_models()
    (installed() / STAMP).unlink()
    _, report = init(setup_repo, monkeypatch, capsys)
    assert "an earlier agentic" in item(report, "policy: standalone")["detail"]


def backups() -> list[Path]:
    return sorted(installed().parent.glob("standalone.previous-*"))


def test_refresh_template_takes_the_new_template_and_keeps_policy_edits(
    setup_repo, deps, monkeypatch, capsys, tmp_path
):
    """DF-273: an upgraded CLI's template replaces the copy; the person's policy/ files
    stay; the old copy is kept whole beside it, never deleted."""
    init(setup_repo, monkeypatch, capsys)
    edit_models()
    edited = (installed() / "policy" / "tiers.yaml").read_text(encoding="utf-8")
    agent = installed() / "agents" / "developer.md"
    agent.write_text(agent.read_text(encoding="utf-8") + "\nMy local tweak.\n", "utf-8")
    newer = _newer_framework(tmp_path)
    monkeypatch.setenv("AGENTIC_FRAMEWORK_ROOT", str(newer))

    code, report = init(setup_repo, monkeypatch, capsys, "--refresh-template")

    assert code == ExitCode.OK, report
    assert (installed() / "policy" / "tiers.yaml").read_text(encoding="utf-8") == edited
    text = (installed() / "agents" / "developer.md").read_text(encoding="utf-8")
    assert "A newer instruction" in text and "My local tweak" not in text
    stamp = json.loads((installed() / STAMP).read_text(encoding="utf-8"))
    assert stamp["template"] == template_digest(newer)
    [backup] = backups()
    assert "My local tweak" in (backup / "agents" / "developer.md").read_text(encoding="utf-8")
    detail = item(report, "policy: standalone")["detail"]
    assert str(backup) in detail and "agents/developer.md" in detail
    assert "policy/tiers.yaml" in detail
    assert [p.name for p in installed().parent.iterdir() if p.name.startswith(".")] == []
    _, again = init(setup_repo, monkeypatch, capsys)
    assert item(again, "policy: standalone") is None  # the copy is now the current template


def test_refresh_template_with_the_current_template_changes_nothing(
    setup_repo, deps, monkeypatch, capsys
):
    init(setup_repo, monkeypatch, capsys)
    edit_models()
    code, report = init(setup_repo, monkeypatch, capsys, "--refresh-template")
    assert code == ExitCode.OK, report
    assert "nothing to refresh" in item(report, "policy: standalone")["detail"]
    assert backups() == []


def test_a_guard_refusal_refreshes_nothing(setup_repo, deps, monkeypatch, capsys, tmp_path):
    init(setup_repo, monkeypatch, capsys)
    edit_models()
    monkeypatch.setenv("AGENTIC_FRAMEWORK_ROOT", str(_newer_framework(tmp_path)))
    (setup_repo / "sub").mkdir()
    code, _ = init(setup_repo / "sub", monkeypatch, capsys, "--refresh-template")
    assert code == ExitCode.REFUSED
    assert backups() == []
    assert "A newer instruction" not in (installed() / "agents" / "developer.md").read_text(
        encoding="utf-8"
    )


def test_refresh_template_is_only_for_the_standalone_template(
    setup_repo, deps, monkeypatch, capsys, tmp_path
):
    monkeypatch.chdir(setup_repo)
    assert main(["--json", "init", "--refresh-template"]) == ExitCode.USAGE
    other = tmp_path / "mine"
    shutil.copytree(framework_root() / "templates" / "standalone" / "policy", other / "policy")
    code = main(["--json", "init", "--standalone", "--policy", str(other), "--refresh-template"])
    assert code == ExitCode.USAGE
    assert "--refresh-template" in capsys.readouterr().err
    assert main(["--json", "init", "--repo", "--standalone", "--refresh-template"]) == (
        ExitCode.USAGE
    )


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
    # The only problem left is the publish step's missing GitHub token (M1-58): a warning,
    # because the step runs only `when` the work passed, so the dry run itself passes.
    problems = [(p["step"], p["check"], p["severity"]) for p in plan["preflight"]["problems"]]
    assert problems == [("publish", "publish", "warning")], plan["preflight"]
    assert "agentic login --for github" in plan["preflight"]["problems"][0]["detail"]
    assert code == ExitCode.OK


def test_a_standalone_plan_does_not_claim_an_org(setup_repo, deps, monkeypatch, capsys):
    """With no org configured the policy repo's layer is shown as 'policy' (DF-286)."""
    init(setup_repo, monkeypatch, capsys)
    edit_models()
    monkeypatch.setenv("AGENTIC_USER", USER)
    monkeypatch.setenv("AGENTIC_TRACES", "off")
    monkeypatch.setattr(runtime, "machine_readiness", lambda: Readiness("macos", ()))
    main(["--no-color", "run", "feature-delivery", "--input", "request=x", "--dry-run"])
    printed = capsys.readouterr().out
    assert "(policy)" in printed and "(org)" not in printed and ", org)" not in printed


def test_a_custom_policy_is_remembered_for_this_repo(
    setup_repo, deps, policy_repo, monkeypatch, capsys
):
    """`init --standalone --policy X` (even a relative X) is what later commands in this
    repo use, with nothing to set by hand (DF-274)."""
    relative = os.path.relpath(policy_repo, setup_repo)  # init runs from the repo root
    _, report = init(setup_repo, monkeypatch, capsys, "--policy", relative)
    runs = item(report, "policy: runs")
    assert runs["status"] == "info", runs
    found = Workspace.load(setup_repo, env={}, home=agentic_home()).policy_repo
    assert found is not None and found.resolve() == policy_repo.resolve()


def test_init_again_without_policy_keeps_the_remembered_one(
    setup_repo, deps, policy_repo, monkeypatch, capsys
):
    """Re-running `init --standalone` to refresh readiness must not swap a custom policy
    for the template behind the person's back."""
    init(setup_repo, monkeypatch, capsys, "--policy", str(policy_repo))
    _, report = init(setup_repo, monkeypatch, capsys)
    assert str(policy_repo) in item(report, "policy")["detail"]
    kept = item(report, "policy: standalone")
    assert kept["status"] == "info" and "--policy" in kept["detail"]
    assert not installed().exists()  # the template was not installed in its place
    found = Workspace.load(setup_repo, env={}, home=agentic_home()).policy_repo
    assert found is not None and found.resolve() == policy_repo.resolve()


def test_init_again_when_the_remembered_policy_is_gone_says_so(
    setup_repo, deps, policy_repo, tmp_path, monkeypatch, capsys
):
    moved = tmp_path / "moved-policy"
    shutil.copytree(policy_repo, moved)
    init(setup_repo, monkeypatch, capsys, "--policy", str(moved))
    shutil.rmtree(moved)
    _, report = init(setup_repo, monkeypatch, capsys)
    gone = item(report, "policy: standalone")
    assert gone["status"] == "warn" and str(moved) in gone["detail"], report["checks"]
    assert "--policy" in gone["fix"]
    assert str(installed()) in item(report, "policy")["detail"]  # the template, said aloud


def test_a_policy_later_commands_will_not_find_is_reported(
    setup_repo, deps, policy_repo, tmp_path, monkeypatch, capsys
):
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("AGENTIC_POLICY_REPO", str(other))  # an explicit setting wins
    _, report = init(setup_repo, monkeypatch, capsys, "--policy", str(policy_repo))
    runs = item(report, "policy: runs")
    assert runs["status"] == "warn" and "AGENTIC_POLICY_REPO" in runs["fix"]
    assert str(other) in runs["detail"]


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


def test_a_failed_install_leaves_no_partial_policy(home, deps, monkeypatch):
    """The scratch folder and ~/.agentic can be on different volumes (a redirected
    profile, AGENTIC_HOME on another drive): the copy can fail part-way. A half-copied
    standalone/ would be taken as installed and never rebuilt (DF-281)."""
    from agentic_cli.policy_source import resolve_policy

    loaded = resolve_policy(standalone=True, policy_path=None, home=agentic_home(), config={})
    copied, copytree = [], shutil.copytree

    def copy_then_fail(src, dst, **kwargs):
        if len(copied) >= 5:
            raise OSError(28, "No space left on device")
        copied.append(src)
        return shutil.copyfile(src, dst)

    def copytree_failing(src, dst, *args, **kwargs):
        if args:  # copytree's own recursion: our copy function is already in args
            return copytree(src, dst, *args, **kwargs)
        return copytree(src, dst, **{**kwargs, "copy_function": copy_then_fail})

    def other_volume(src, dst):
        raise OSError(18, "Invalid cross-device link")

    with monkeypatch.context() as patched, pytest.raises(OSError):
        patched.setattr(shutil, "copytree", copytree_failing)
        patched.setattr(os, "rename", other_volume)
        loaded.commit()
    assert not installed().exists()
    assert [p.name for p in installed().parent.iterdir()] == []  # no temporary folder left
    loaded.discard()


def test_a_rename_blocked_for_a_moment_is_retried(home, deps, monkeypatch):
    """On Windows an antivirus scanner holds handles on files just copied, so renaming
    their folder fails with access denied for a moment: init retries, not fails."""
    from agentic_cli import policy_source
    from agentic_cli.policy_source import resolve_policy

    loaded = resolve_policy(standalone=True, policy_path=None, home=agentic_home(), config={})
    replace, calls = os.replace, []

    def held_once(src, dst):
        calls.append(src)
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied")
        return replace(src, dst)

    monkeypatch.setattr(policy_source.os, "replace", held_once)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    loaded.commit()
    assert len(calls) == 2 and (installed() / "policy" / "tiers.yaml").is_file()


def test_a_rename_that_stays_blocked_says_what_to_do(home, deps, monkeypatch):
    from agentic_cli import policy_source
    from agentic_cli.errors import CliError
    from agentic_cli.policy_source import resolve_policy

    loaded = resolve_policy(standalone=True, policy_path=None, home=agentic_home(), config={})

    def held(src, dst):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(policy_source.os, "replace", held)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    with pytest.raises(CliError) as caught:
        loaded.commit()
    assert caught.value.code == ExitCode.ERROR and str(installed()) in str(caught.value)
    assert "run `agentic init` again" in caught.value.hint
    assert not installed().exists()
    assert [p.name for p in installed().parent.iterdir()] == []  # no temporary folder left
    loaded.discard()


def test_standalone_reads_the_same_local_policy_keys_as_later_commands(home, policy_repo):
    from agentic_cli.policy_source import resolve_policy

    for key in ("policy", "policy_repo"):
        config = {key: str(policy_repo)}
        loaded = resolve_policy(standalone=True, policy_path=None, home=home, config=config)
        assert loaded.root == policy_repo, key
    org = {"org": "acme", "policy_repo": "https://github.com/acme/agentic-org-policy.git"}
    loaded = resolve_policy(standalone=True, policy_path=None, home=home, config=org)
    try:  # an org's policy_repo is a fetch source, not a local policy: the template
        assert loaded.source == str(home / STANDALONE_DIR)
    finally:
        loaded.discard()


def test_a_copy_installed_meanwhile_is_kept(home, deps, tmp_path):
    from agentic_cli.policy_source import resolve_policy

    loaded = resolve_policy(standalone=True, policy_path=None, home=agentic_home(), config={})
    theirs = installed()
    theirs.parent.mkdir(parents=True)
    shutil.copytree(loaded.root, theirs)  # another init finished first
    (theirs / "mine.txt").write_text("kept\n", encoding="utf-8")
    loaded.commit()
    assert loaded.root == theirs and (theirs / "mine.txt").is_file()
    assert not (theirs / "repo").exists()  # nothing moved inside it
