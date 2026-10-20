"""Tests for the secret-scan policy: block or mask what is about to leave for a model (M1-41)."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, SecretScanPolicy, load_policy


def policy_copy(fixtures: Path, tmp_path: Path, org_extra: str) -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    org = root / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + org_extra, encoding="utf-8")
    return root


def test_the_default_blocks_with_the_default_detectors(fixtures):
    policy = load_policy(fixtures / "policy" / "policy")
    assert policy.secret_scan == SecretScanPolicy()
    assert policy.secret_scan.action == "block"
    assert policy.secret_scan.detectors is None


def test_mask_loads_from_org_yaml(fixtures, tmp_path):
    root = policy_copy(fixtures, tmp_path, "secret_scan:\n  action: mask\n")
    assert load_policy(root).secret_scan.action == "mask"


def test_detectors_narrow_the_set(fixtures, tmp_path):
    extra = "secret_scan:\n  detectors: [AWSKeyDetector, PrivateKeyDetector]\n"
    policy = load_policy(policy_copy(fixtures, tmp_path, extra))
    assert policy.secret_scan.detectors == ["AWSKeyDetector", "PrivateKeyDetector"]


def test_an_unknown_action_is_an_error(fixtures, tmp_path):
    root = policy_copy(fixtures, tmp_path, "secret_scan:\n  action: warn\n")
    with pytest.raises(PolicyError, match="secret_scan"):
        load_policy(root)


def test_an_unknown_detector_is_an_error_naming_it(fixtures, tmp_path):
    root = policy_copy(fixtures, tmp_path, "secret_scan:\n  detectors: [NoSuchDetector]\n")
    with pytest.raises(PolicyError, match="NoSuchDetector"):
        load_policy(root)


def test_an_empty_detector_list_is_an_error(fixtures, tmp_path):
    # an empty list would switch the scan off without saying so
    root = policy_copy(fixtures, tmp_path, "secret_scan:\n  detectors: []\n")
    with pytest.raises(PolicyError, match="detectors"):
        load_policy(root)
