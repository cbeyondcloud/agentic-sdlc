"""Approved pipeline hashes (D33) and the org minimum CLI version (M2-08): one function for
the control plane's sync and the CLI's cache, so both hash the same bytes alike."""

import hashlib
import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, load_policy
from agentic.policy.load import pipeline_digests

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


def test_min_cli_version_is_read_from_org_yaml(tmp_path):
    root = tmp_path / "policy"
    shutil.copytree(FIXTURE / "policy", root)
    org = root / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + "min_cli_version: 0.4.0\n", encoding="utf-8")
    assert load_policy(root).min_cli_version == "0.4.0"


def test_min_cli_version_must_look_like_a_version(tmp_path):
    root = tmp_path / "policy"
    shutil.copytree(FIXTURE / "policy", root)
    org = root / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + "min_cli_version: latest\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="min_cli_version"):
        load_policy(root)
