"""The review pre-pass scanners (M2-48) through Zyroflo's `zyroflo.review` and the verified
tool cache (M1-51). No real scanner runs here: Zyroflo's `run_tool` seam answers as the
tools do, so the parse paths are Zyroflo's own. A missing or refused tool is a stated
status with its reason, never a clean result."""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from zyroflo.review import prepass as zyroflo_prepass
from zyroflo.review import sca as zyroflo_sca

from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.review_prepass import OFFLINE_FLAGS, ToolLocator, external_checks
from sdlc_commons.worktrees import StepCheckpoints, change_since_base

LINUX = "linux-x86_64"
WINDOWS = "windows-x86_64"
AWS = "AKIA" + "Z7Q4R2XW" + "9PLM3TKB"


@dataclass(frozen=True)
class Artefact:
    file: str
    sha256: str = "0" * 64
    signed: bool = False


@dataclass(frozen=True)
class Pin:
    version: str
    binary: str
    assets: dict = field(default_factory=dict)


class Cache:
    """The fetcher's cache as the locator asks it: the verified binary, or None."""

    def __init__(self, root: Path, installed: set[str]) -> None:
        self.root, self.installed = root, installed

    def cached(self, name: str, pin: Pin) -> Path | None:
        if name not in self.installed:
            return None
        path = self.root / name
        path.write_text("not a binary", encoding="utf-8")
        return path


def pins(*names: str, platform: str = LINUX, signed: bool = False) -> dict[str, Pin]:
    return {n: Pin("1.0", n, {platform: Artefact(f"{n}.tgz", signed=signed)}) for n in names}


ALL = ("gitleaks", "ast-grep", "osv-scanner")


def locator(tmp_path: Path, installed=ALL, platform: str = LINUX, **kwargs) -> ToolLocator:
    cache = Cache(tmp_path, set(installed))
    return ToolLocator(pins(*ALL, platform=platform, **kwargs), cache, platform)


def diff_for(path: str, line: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,1 +1,2 @@\n context\n+{line}\n"
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "settings.py").write_text(f'context\nKEY = "{AWS}"\n', encoding="utf-8")
    (root / "requirements.txt").write_text("context\njinja2==2.4.1\n", encoding="utf-8")
    return root


class Tools:
    """Answers as gitleaks, ast-grep and osv-scanner do; records every command."""

    def __init__(self, vulnerable: bool = True) -> None:
        self.commands: list[list[str]] = []
        self.vulnerable = vulnerable

    def __call__(self, cmd, timeout=300, env=None):
        self.commands.append(list(cmd))
        if cmd[1] == "dir":  # gitleaks: report file, leak exit
            report = Path(cmd[cmd.index("--report-path") + 1])
            found = [
                {
                    "Description": "AWS Access Key",
                    "RuleID": "aws-access-token",
                    "File": str(Path(cmd[2]) / "settings.py"),
                    "StartLine": 2,
                    "StartColumn": 8,
                    "Match": f'KEY = "{AWS}"',
                    "Secret": AWS,
                    "Fingerprint": "settings.py:aws-access-token:2",
                }
            ]
            report.write_text(json.dumps(found), encoding="utf-8")
            return 9, "", ""
        if cmd[1] == "scan" and cmd[2] == "--inline-rules":  # ast-grep: clean
            return 0, "[]", ""
        if cmd[1:3] == ["scan", "source"]:  # osv-scanner
            where = Path(cmd[-1])
            log = f"Scanned {where / 'requirements.txt'} file and found 1 package"
            if not self.vulnerable:
                return 0, json.dumps({"results": None}), log
            report = {
                "results": [
                    {
                        "source": {"path": str(where / "requirements.txt")},
                        "packages": [
                            {
                                "package": {
                                    "name": "jinja2",
                                    "version": "2.4.1",
                                    "ecosystem": "PyPI",
                                },
                                "groups": [
                                    {
                                        "ids": ["GHSA-462w-v97r-4m45"],
                                        "aliases": ["CVE-2019-10906"],
                                        "max_severity": "8.6",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
            return 1, json.dumps(report), log
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def tools(monkeypatch) -> Tools:
    fake = Tools()
    monkeypatch.setattr(zyroflo_prepass, "run_tool", fake)
    return fake


def row(document: dict, name: str) -> dict:
    return next(t for t in document["tools"] if t["name"] == name)


def diagnostics(document: dict, name: str) -> list[dict]:
    return [d for s in document["sources"] if s["source"]["name"] == name for d in s["diagnostics"]]


def change() -> str:
    return diff_for("settings.py", f'KEY = "{AWS}"') + diff_for("requirements.txt", "jinja2==2.4.1")


def test_missing_tool_is_not_run_with_reason(tmp_path, tree, tools):
    document = external_checks(tree, change(), locator(tmp_path, installed=()))
    for name in ALL:
        assert row(document, name)["status"] == "not-run"
        assert "agentic init" in row(document, name)["detail"]
    assert tools.commands == []  # nothing ran


def test_unpinned_tool_says_so(tmp_path):
    located = ToolLocator({}, Cache(tmp_path, set()), LINUX).locate("gitleaks")
    assert located[0] is None and "pins no gitleaks" in located[1]


def test_no_build_for_this_platform_says_so(tmp_path):
    found = ToolLocator(pins("gitleaks", platform=LINUX), Cache(tmp_path, set()), "darwin-aarch64")
    exe, why = found.locate("gitleaks")
    assert exe is None and "darwin-aarch64" in why


def test_unsigned_windows_build_is_skipped_on_windows(tmp_path, tree, tools):
    document = external_checks(tree, change(), locator(tmp_path, platform=WINDOWS))
    detail = row(document, "gitleaks")["detail"]
    assert row(document, "gitleaks")["status"] == "not-run"
    assert detail.startswith("skipped on Windows") and "signed" in detail
    assert tools.commands == []


def test_signed_windows_build_runs(tmp_path, tree, tools):
    document = external_checks(tree, change(), locator(tmp_path, platform=WINDOWS, signed=True))
    assert row(document, "gitleaks")["status"] == "ok"


def test_gitleaks_finding_through_zyroflo_is_redacted(tmp_path, tree, tools):
    document = external_checks(tree, change(), locator(tmp_path))
    assert row(document, "gitleaks") | {"own_trees": 0} == row(document, "gitleaks")
    (finding,) = diagnostics(document, "gitleaks")
    assert finding["code"]["value"] == "aws-access-token"
    assert finding["location"]["path"] == "settings.py"
    assert AWS not in json.dumps(document)
    assert row(document, "ast-grep")["status"] == "ok"


def test_known_vulnerable_dependency_is_a_finding(tmp_path, tree, tools):
    document = external_checks(tree, change(), locator(tmp_path))
    assert row(document, "osv-scanner")["status"] == "ok"
    (finding,) = diagnostics(document, "osv-scanner")
    assert finding["location"]["path"] == "requirements.txt"
    assert "CVE-2019-10906" in finding["message"] and finding["severity_class"] == "high"


def test_osv_runs_offline_unless_policy_opts_in(tmp_path, tree, tools):
    external_checks(tree, change(), locator(tmp_path))
    (offline,) = [c for c in tools.commands if c[1:3] == ["scan", "source"]]
    assert all(flag in offline for flag in OFFLINE_FLAGS)
    tools.commands.clear()
    document = external_checks(tree, change(), locator(tmp_path), online=True)
    (online,) = [c for c in tools.commands if c[1:3] == ["scan", "source"]]
    assert not any(flag in online for flag in OFFLINE_FLAGS)
    assert row(document, "osv-scanner")["mode"] == "online"


#: Files a change could add to steer a scanner: its config or ignore list.
STEERING = {
    ".gitleaks.toml": "[allowlist]\npaths = ['''.*''']\n",
    ".gitleaksignore": "settings.py:aws-access-token:2\n",
    "osv-scanner.toml": '[[IgnoredVulns]]\nid = "GHSA-462w-v97r-4m45"\n',
    ".ignore": "*\n",
}


class SeeingTools(Tools):
    """`Tools`, noting each command's environment and what its scan root holds."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[list[str], dict, set[str]]] = []

    def __call__(self, cmd, timeout=300, env=None):
        root = Path(cmd[2] if cmd[1] == "dir" else cmd[-1])
        held = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        config = {}
        if env and env.get("GITLEAKS_CONFIG"):
            config["gitleaks"] = Path(env["GITLEAKS_CONFIG"]).read_text(encoding="utf-8")
        if "--config" in cmd:
            config["osv"] = Path(cmd[cmd.index("--config") + 1]).read_text(encoding="utf-8")
        self.seen.append((list(cmd), {**(env or {}), **config}, held))
        return super().__call__(cmd, timeout, env)


def test_a_change_cannot_configure_the_scanners(tmp_path, tree, monkeypatch):
    fake = SeeingTools()
    monkeypatch.setattr(zyroflo_prepass, "run_tool", fake)
    text = change()
    for name, body in STEERING.items():
        (tree / name).write_text(body, encoding="utf-8")
        (tree / "sub").mkdir(exist_ok=True)
        (tree / "sub" / name).write_text(body, encoding="utf-8")
        text += diff_for(name, body.splitlines()[0]) + diff_for(f"sub/{name}", "x")
    document = external_checks(tree, text, locator(tmp_path))
    assert row(document, "gitleaks")["findings"] == 1
    for cmd, _env, held in fake.seen:
        steering = {h for h in held if h.rsplit("/", 1)[-1] in STEERING}
        assert steering == set(), f"{cmd[1]} saw the change's own {sorted(steering)}"
    gitleaks = [env for cmd, env, _ in fake.seen if cmd[1] == "dir"]
    assert gitleaks and all("useDefault = true" in env["gitleaks"] for env in gitleaks)
    osv = [(cmd, env) for cmd, env, _ in fake.seen if cmd[1:3] == ["scan", "source"]]
    assert osv and all("IgnoredVulns" not in env["osv"] for _, env in osv)
    assert all(cmd[-1] != cmd[cmd.index("--config") + 1] for cmd, _ in osv)


def test_shim_restores_zyroflo_functions(tmp_path, tree, monkeypatch):
    def broken(cmd, timeout=300, env=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(zyroflo_prepass, "run_tool", broken)
    binary, run_tool = zyroflo_prepass.binary, zyroflo_prepass.run_tool
    diff_scope = zyroflo_prepass.diff_scope
    with pytest.raises(RuntimeError):
        external_checks(tree, change(), locator(tmp_path))
    assert zyroflo_prepass.binary is binary and zyroflo_prepass.run_tool is run_tool
    assert zyroflo_prepass.diff_scope is diff_scope
    assert zyroflo_sca.prepass is zyroflo_prepass


GIT_USER = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT_USER, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def test_change_since_base_is_the_runs_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    RunLedger(repo, "r-pre-1").open(
        identity=RunIdentity(team="platform", policy_version="git:" + "a" * 40),
        kind="feature",
        project="demo",
        owner="platform",
        criticality="low",
    )
    checkpoints = StepCheckpoints(repo, "r-pre-1")
    work = checkpoints.prepare()
    checkpoints.begin(fresh=True)
    (work / "app.py").write_text("x = 2\n", encoding="utf-8")
    checkpoints.finish("agentic: develop #1")
    ((component, path, text),) = change_since_base(repo, "r-pre-1")
    assert component == "repo" and path == work
    assert "-x = 1" in text and "+x = 2" in text and "diff --git a/app.py b/app.py" in text
