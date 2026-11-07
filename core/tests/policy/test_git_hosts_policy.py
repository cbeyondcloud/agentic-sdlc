"""`org.yaml: allowed_git_hosts` (DF-484, DF-503): the git hosts the control plane may fetch
from. Optional: a policy without it loads as before (None, the control plane's default)."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, load_policy
from agentic.policy.repos import git_host_key


def policy_copy(fixtures: Path, tmp_path: Path, org_extra: str) -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    org = root / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + org_extra, encoding="utf-8")
    return root


def test_unset_is_none(fixtures):
    assert load_policy(fixtures / "policy" / "policy").allowed_git_hosts is None


def test_hosts_are_read_and_normalised(fixtures, tmp_path):
    extra = "allowed_git_hosts: [GitHub.com, 'ghe.acme.example.', '[::1]', 10.0.0.5]\n"
    policy = load_policy(policy_copy(fixtures, tmp_path, extra))
    assert policy.allowed_git_hosts == ["github.com", "ghe.acme.example", "::1", "10.0.0.5"]


@pytest.mark.parametrize(
    "entry",
    ["https://github.com", "github.com:443", "github.com/acme", "*.acme.example", "git@x", ""],
)
def test_an_entry_that_is_not_a_bare_host_is_refused(fixtures, tmp_path, entry):
    root = policy_copy(fixtures, tmp_path, f"allowed_git_hosts: ['{entry}']\n")
    with pytest.raises(PolicyError):
        load_policy(root)


def test_git_host_key():
    # one address, however it is written, is one key
    assert git_host_key("[::FFFF:7F00:1]") == git_host_key("::ffff:127.0.0.1")
    with pytest.raises(ValueError):
        git_host_key("-bad.example")
