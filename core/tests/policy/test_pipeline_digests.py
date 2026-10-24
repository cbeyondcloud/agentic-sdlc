"""Approved pipeline hashes (D33) and the org minimum CLI version (M2-08): one function for
the control plane's sync and the CLI's cache, so both hash the same bytes alike."""

import hashlib
import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, load_policy
from agentic.policy.load import overlay_index, pipeline_digests

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "policy"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_org_and_team_pipelines_are_hashed_by_policy_repo_path(tmp_path):
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "deliver.yaml").write_bytes(b"name: deliver\r\n")
    (tmp_path / "teams" / "payments" / "pipelines").mkdir(parents=True)
    (tmp_path / "teams" / "payments" / "pipelines" / "hotfix.yaml").write_bytes(b"name: h\n")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "ba.md").write_text("not a pipeline", encoding="utf-8")
    assert pipeline_digests(tmp_path) == {
        "pipelines/deliver.yaml": _sha(b"name: deliver\r\n"),  # the bytes as they are
        "teams/payments/pipelines/hotfix.yaml": _sha(b"name: h\n"),
    }


def test_no_pipelines_gives_an_empty_map(tmp_path):
    assert pipeline_digests(tmp_path) == {}


def test_digests_cover_archived_versions(tmp_path):
    """M2-15: a team held back by a rollout (or pinned) runs an archived pipeline version;
    it is an approved pipeline too, by its path in the policy repo."""
    archive = tmp_path / "pipelines" / "versions" / "deliver"
    archive.mkdir(parents=True)
    (archive / "1.0.0.yaml").write_bytes(b"name: deliver\nversion: 1.0.0\n")
    team = tmp_path / "teams" / "payments" / "pipelines" / "versions" / "hotfix"
    team.mkdir(parents=True)
    (team / "2.0.0.yaml").write_bytes(b"name: hotfix\n")
    overlays = tmp_path / "teams" / "payments" / "pipelines" / "overlays"
    overlays.mkdir(parents=True)
    (overlays / "app.yaml").write_bytes(b"pipelines: {}\n")
    assert pipeline_digests(tmp_path) == {
        "pipelines/versions/deliver/1.0.0.yaml": _sha(b"name: deliver\nversion: 1.0.0\n"),
        "teams/payments/pipelines/versions/hotfix/2.0.0.yaml": _sha(b"name: hotfix\n"),
    }  # an overlay is not a pipeline: `overlay_index` lists it


def test_overlay_index_lists_overlays(tmp_path):
    """M2-23 (D33): every approved overlay with the digest of its bytes, its team, the
    repository it binds (None: any repository of that name) and the pipelines it changes."""
    overlays = tmp_path / "teams" / "payments" / "pipelines" / "overlays"
    overlays.mkdir(parents=True)
    bound = b"repo: git.example.com/acme/App\npipelines:\n  deliver: {remove: [design]}\n"
    (overlays / "app.yaml").write_bytes(bound)
    crlf = b"pipelines:\r\n  deliver: {max_minutes: 9}\r\n"
    (overlays / "web.yaml").write_bytes(crlf)
    (overlays / "broken.yaml").write_bytes(b"pipelines: [\n")  # the sync rejects it
    (tmp_path / "pipelines" / "overlays").mkdir(parents=True)  # never read: org layer
    (tmp_path / "pipelines" / "overlays" / "app.yaml").write_bytes(b"pipelines: {}\n")
    assert overlay_index(tmp_path) == {
        "teams/payments/pipelines/overlays/app.yaml": {
            "sha256": _sha(bound),
            "team": "payments",
            "repo": "git.example.com/acme/App",
            "pipelines": ["deliver"],
        },
        "teams/payments/pipelines/overlays/web.yaml": {
            "sha256": _sha(crlf),
            "team": "payments",
            "repo": None,
            "pipelines": ["deliver"],
        },
    }


def test_min_cli_version_is_read_from_org_yaml(tmp_path):
    root = tmp_path / "policy"
    shutil.copytree(FIXTURE / "policy", root)
    org = root / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + "min_cli_version: 0.4.0\n", encoding="utf-8")
    assert load_policy(root).min_cli_version == "0.4.0"


@pytest.mark.parametrize("value", ["latest", "1.2.x", "1.2abc", "1.2-x-y", "1.0+local"])
def test_min_cli_version_must_be_a_version_the_control_plane_can_compare(tmp_path, value):
    """Review F4: a value the control plane's PEP 440 comparison would drop (so no minimum
    is enforced) is refused when the policy loads, and so the version is rejected."""
    root = tmp_path / "policy"
    shutil.copytree(FIXTURE / "policy", root)
    org = root / "org.yaml"
    text = org.read_text(encoding="utf-8") + f"min_cli_version: '{value}'\n"
    org.write_text(text, encoding="utf-8")
    with pytest.raises(PolicyError, match="min_cli_version"):
        load_policy(root)


@pytest.mark.parametrize("value", ["1", "0.4.0", "1.2.3.4", "2.0.0rc1", "1.0.post2", "1.1.dev3"])
def test_min_cli_version_accepts_release_pre_post_and_dev_versions(tmp_path, value):
    root = tmp_path / "policy"
    shutil.copytree(FIXTURE / "policy", root)
    org = root / "org.yaml"
    text = org.read_text(encoding="utf-8") + f"min_cli_version: '{value}'\n"
    org.write_text(text, encoding="utf-8")
    assert load_policy(root).min_cli_version == value
