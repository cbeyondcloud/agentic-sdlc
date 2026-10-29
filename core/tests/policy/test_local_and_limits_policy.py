"""Policy for the laptop's own housekeeping (M3-08: `limits.yaml` `local:`) and the sandbox's
resource limits per agent command (M3-10: `org.yaml` `sandbox.limits:`). Both default to
what an existing policy repo had: no setting changes nothing."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, load_policy
from agentic.policy.model import LocalDataPolicy, ResourceLimits


def policy_copy(fixtures: Path, tmp_path: Path, file: str, extra: str) -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    target = root / file
    before = target.read_text(encoding="utf-8") if target.is_file() else ""
    target.write_text(before + extra, encoding="utf-8")
    return root


def test_defaults_when_unset(fixtures: Path):
    policy = load_policy(fixtures / "policy" / "policy")
    assert policy.local_data == LocalDataPolicy()
    assert policy.local_data.gc_after_days == 14
    assert policy.local_data.disk_cap_gb == 20
    assert policy.sandbox.limits == ResourceLimits()
    assert policy.sandbox.limits.given() == {}


def test_local_settings_are_read_from_limits_yaml(fixtures: Path, tmp_path: Path):
    root = policy_copy(
        fixtures, tmp_path, "limits.yaml", "\nlocal: { gc_after_days: 7, disk_cap_gb: 5.5 }\n"
    )
    policy = load_policy(root)
    assert policy.local_data.gc_after_days == 7
    assert policy.local_data.disk_cap_gb == 5.5


@pytest.mark.parametrize("bad", ["gc_after_days: 0", "disk_cap_gb: 0", "keep: 3"])
def test_bad_local_settings_are_refused(fixtures: Path, tmp_path: Path, bad: str):
    root = policy_copy(fixtures, tmp_path, "limits.yaml", f"\nlocal: {{ {bad} }}\n")
    with pytest.raises(PolicyError):
        load_policy(root)


def test_sandbox_limits_are_read_from_org_yaml(fixtures: Path, tmp_path: Path):
    extra = (
        "\nsandbox:\n  limits: { cpu_seconds: 600, memory_mb: 4096, max_file_mb: 512, "
        "processes: 256 }\n"
    )
    root = policy_copy(fixtures, tmp_path, "org.yaml", extra)
    limits = load_policy(root).sandbox.limits
    assert limits.given() == {
        "cpu_seconds": 600,
        "memory_mb": 4096,
        "max_file_mb": 512,
        "processes": 256,
    }


@pytest.mark.parametrize("bad", ["cpu_seconds: 0", "memory_mb: -1", "disk_mb: 10"])
def test_bad_sandbox_limits_are_refused(fixtures: Path, tmp_path: Path, bad: str):
    root = policy_copy(fixtures, tmp_path, "org.yaml", f"\nsandbox:\n  limits: {{ {bad} }}\n")
    with pytest.raises(PolicyError):
        load_policy(root)
