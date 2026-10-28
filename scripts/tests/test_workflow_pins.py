"""GitHub Actions are pinned by commit SHA (DF-689): a moved tag must never run code the
org did not review. Covers the workflows the framework ships to org repos, the release
template (M3-21), and this repo's own (unused: CI here is local, scripts/check.py)."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check import WORKFLOW_TEMPLATES  # noqa: E402

from agentic.yamlio import load_yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted(
    {
        *WORKFLOW_TEMPLATES,
        *(p.relative_to(ROOT).as_posix() for p in ROOT.glob(".github/workflows/*.yml")),
    }
)
RELEASE = ROOT / "templates" / "release" / "release.yml"
PINNED = re.compile(
    r"^\s*-?\s*uses:\s*(?P<action>[\w.-]+/[\w./-]+)@(?P<sha>[0-9a-f]{40})\s+#\s*v\d+\.\d+\.\d+\s*$"
)


def uses_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if "uses:" in ln]


def test_the_release_template_is_linted_by_check():
    assert "templates/release/release.yml" in WORKFLOW_TEMPLATES


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_every_action_is_pinned_by_sha_with_its_version(rel: str):
    lines = uses_lines(ROOT / rel)
    assert lines, rel
    for line in lines:
        assert PINNED.match(line), f"{rel}: {line.strip()}"


def test_one_sha_per_action_across_the_shipped_templates():
    seen: dict[str, set[str]] = {}
    for rel in WORKFLOW_TEMPLATES:
        for line in uses_lines(ROOT / rel):
            found = PINNED.match(line)
            seen.setdefault(found["action"], set()).add(found["sha"])
    assert {action: shas for action, shas in seen.items() if len(shas) > 1} == {}


@pytest.fixture(scope="module")
def release() -> dict:
    data, _ = load_yaml(RELEASE.read_text(encoding="utf-8"))
    return data


def steps(workflow: dict):
    for job in workflow["jobs"].values():
        yield from job["steps"]


def test_release_runs_the_local_release_script(release: dict):
    runs = "\n".join(step.get("run", "") for step in steps(release))
    assert 'uv run python scripts/release.py "${args[@]}"' in runs
    assert "args=(release " in runs
    assert "--tag" in runs  # the tag must be the version
    assert "uv sync --locked" in runs


def _bash() -> str | None:
    """A real bash: on Windows Git Bash (next to git, or the default install), never
    System32's WSL launcher or the WindowsApps alias."""
    if sys.platform != "win32":
        return shutil.which("bash")
    git = shutil.which("git")
    candidates = [Path(git).parent.parent / "bin" / "bash.exe"] if git else []
    candidates.append(Path(r"C:\Program Files\Git\bin\bash.exe"))
    return next((str(path) for path in candidates if path.is_file()), None)


@pytest.mark.parametrize(
    ("publish", "tag", "expected"),
    [
        ("1", "v1.2.3", ["--tag", "v1.2.3"]),  # a tag push publishes
        ("1", "", []),  # by hand, with publish on
        ("", "", ["--no-publish"]),  # by hand, with publish off
    ],
)
def test_release_step_runs_a_command_release_py_accepts(
    release: dict, tmp_path: Path, publish: str, tag: str, expected: list[str]
):
    """Run the release step's script as Actions does (bash -eo pipefail), with `uv`
    replaced by a stand-in that prints its arguments, and parse them as release.py would."""
    bash = _bash()
    if bash is None:
        pytest.skip("no bash on this machine")
    from release import _parser

    step = next(s for s in steps(release) if "scripts/release.py" in s.get("run", ""))
    script = tmp_path / "step.sh"
    stand_in = "uv() { printf '%s\\n' \"$@\"; }\n"
    script.write_text(stand_in + step["run"], encoding="utf-8", newline="\n")
    env = dict(os.environ, PUBLISH=publish, TAG=tag, RUNNER_TEMP=tmp_path.as_posix())
    done = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", script.name],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr
    argv = done.stdout.splitlines()
    assert argv[:3] == ["run", "python", "scripts/release.py"]
    args = _parser().parse_args(argv[3:])
    assert args.command == "release"
    assert args.no_publish is (publish == "")
    assert args.tag == (tag or None)
    assert [a for a in argv if a in ("--tag", "--no-publish") or a == tag != ""] == expected


def test_release_reads_only_and_keeps_no_checkout_token(release: dict):
    assert release["permissions"] == {"contents": "read"}
    checkout = next(s for s in steps(release) if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["fetch-depth"] == 0  # the full history: changelog and secret scan


def test_release_secrets_come_from_the_org_and_never_reach_the_command_line(release: dict):
    text = RELEASE.read_text(encoding="utf-8")
    for secret in ("AGENTIC_RELEASE_SIGNING_KEY", "UV_PUBLISH_PASSWORD"):
        assert f"secrets.{secret}" in text, secret
    for step in steps(release):
        assert "secrets." not in step.get("run", ""), step.get("name")


def test_gitleaks_skips_only_findings_pinned_to_an_old_commit():
    """The ignore file lets the history scan pass over fake keys old commits held; an
    entry without a commit would also hide a finding in the current tree."""
    entries = [
        line
        for line in (ROOT / ".gitleaksignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert entries
    for entry in entries:
        assert re.fullmatch(r"[0-9a-f]{40}:[^:]+:[a-z0-9-]+:\d+", entry), entry


def test_release_scans_the_whole_history_with_a_checksummed_gitleaks(release: dict):
    """DF-96: the local check scans the working tree only; the release scans history."""
    scan = next(s for s in steps(release) if "gitleaks" in s.get("run", ""))
    assert "sha256sum -c" in scan["run"] and 'gitleaks" git' in scan["run"]
    assert re.fullmatch(r"[0-9a-f]{64}", scan["env"]["GITLEAKS_SHA256"])
