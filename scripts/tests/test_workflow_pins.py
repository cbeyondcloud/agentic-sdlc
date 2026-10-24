"""GitHub Actions are pinned by commit SHA (DF-689): a moved tag must never run code the
org did not review. Covers the workflows the framework ships to org repos, the release
template (M3-21), and this repo's own (unused: CI here is local, scripts/check.py)."""

import re
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
    assert "uv run python scripts/release.py release" in runs
    assert "--tag" in runs  # the tag must be the version
    assert "uv sync --locked" in runs


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
