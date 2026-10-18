"""Tests for loading the org policy folder (the files admins keep in the policy repo)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from agentic.policy import PolicyError, load_policy


@pytest.fixture
def policy_dir(fixtures: Path, tmp_path: Path) -> Path:
    """A private, editable copy of the sample policy folder."""
    target = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", target)
    return target


def test_loads_fixture_policy(policy_dir: Path):
    policy = load_policy(policy_dir)

    assert set(policy.tiers) == {"deep-reasoning", "balanced", "fast", "local"}
    assert policy.tiers["balanced"].model == "anthropic/claude-sonnet"
    assert policy.team_overrides["payments"]["deep-reasoning"].model == "openai/gpt-5"
    assert policy.classifications["restricted"].allowed_backends == ["native"]
    assert policy.default_classification == "internal"
    assert policy.forbidden_tools == ["web_fetch", "git_push"]
    assert policy.deny.shell == ["git push --force", "rm -rf /"]
    assert policy.role_limits["developer"].max.budget_usd == 5
    assert policy.default_role_limits.children_max.max_total == 2
    assert policy.teams["payments"].allowed_tiers == ["balanced", "fast"]
    assert policy.teams["payments"].deny.shell == ["kubectl"]
    assert policy.blocked_licences == ["AGPL-3.0", "GPL-3.0", "SSPL-1.0"]
    assert policy.licence_scan is True


def test_version_is_content_hash_outside_git(policy_dir: Path):
    first = load_policy(policy_dir).version

    (policy_dir / "licences.yaml").write_text("blocked: [AGPL-3.0]\n", encoding="utf-8")
    second = load_policy(policy_dir).version

    assert first.startswith("sha256:")
    assert second.startswith("sha256:")
    assert first != second


def test_version_is_git_commit_when_policy_is_committed(policy_dir: Path):
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=policy_dir, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "policy")
    commit = git("rev-parse", "HEAD")

    assert load_policy(policy_dir).version == f"git:{commit}"

    (policy_dir / "org.yaml").write_text("allow_repo_pipelines: true\n", encoding="utf-8")

    assert load_policy(policy_dir).version.startswith("sha256:")  # uncommitted change


def test_team_cannot_name_unknown_tier(policy_dir: Path):
    (policy_dir / "teams" / "payments.yaml").write_text(
        "allowed_tiers: [balanced, turbo]\n", encoding="utf-8"
    )

    with pytest.raises(PolicyError) as error:
        load_policy(policy_dir)

    assert "turbo" in str(error.value)


def test_default_classification_must_exist(policy_dir: Path):
    text = (policy_dir / "classifications.yaml").read_text(encoding="utf-8")
    (policy_dir / "classifications.yaml").write_text(
        text.replace("default_classification: internal", "default_classification: secret"),
        encoding="utf-8",
    )

    with pytest.raises(PolicyError) as error:
        load_policy(policy_dir)

    assert "secret" in str(error.value)


def test_repo_patterns_and_mirror_loaded(policy_dir: Path):
    policy = load_policy(policy_dir)

    assert policy.allowed_repo_patterns == ["github.com/acme/*"]
    assert policy.tool_mirror_url == "https://artifacts.acme.example/agentic"


def test_allow_repo_pipelines_defaults_false(policy_dir: Path):
    (policy_dir / "org.yaml").unlink()  # no org-wide settings file at all

    assert load_policy(policy_dir).allow_repo_pipelines is False


def test_unknown_policy_file_rejected(policy_dir: Path):
    (policy_dir / "tier.yaml").write_text("tiers: {}\n", encoding="utf-8")  # typo of tiers.yaml

    with pytest.raises(PolicyError) as error:
        load_policy(policy_dir)

    assert "tier.yaml" in str(error.value)


def test_unknown_setting_in_policy_file_rejected(policy_dir: Path):
    text = (policy_dir / "tools.yaml").read_text(encoding="utf-8")
    tools = policy_dir / "tools.yaml"
    tools.write_text(text.replace("forbidden:", "forbiden:"), encoding="utf-8")

    with pytest.raises(PolicyError) as error:
        load_policy(policy_dir)

    assert "tools.yaml: unknown setting 'forbiden'" in str(error.value)


def test_forbidden_tool_must_be_in_catalog(policy_dir: Path):
    text = (policy_dir / "tools.yaml").read_text(encoding="utf-8")
    (policy_dir / "tools.yaml").write_text(
        text.replace("forbidden: [web_fetch, git_push]", "forbidden: [web_fetch, ftp_upload]"),
        encoding="utf-8",
    )

    with pytest.raises(PolicyError) as error:
        load_policy(policy_dir)

    assert "ftp_upload" in str(error.value)


def test_problems_are_reported_one_per_line_under_policy_heading(policy_dir: Path):
    team_file = policy_dir / "teams" / "payments.yaml"
    team_file.write_text("allowed_tiers: [turbo]\n", encoding="utf-8")
    text = (policy_dir / "classifications.yaml").read_text(encoding="utf-8")
    (policy_dir / "classifications.yaml").write_text(
        text.replace("default_classification: internal", "default_classification: secret"),
        encoding="utf-8",
    )

    with pytest.raises(PolicyError) as error:
        load_policy(policy_dir)

    message = str(error.value)
    assert "invalid policy" in message
    assert "\n  - policy: teams.payments.allowed_tiers names unknown tier 'turbo'" in message
    assert "\n  - policy: default_classification 'secret' is not one of" in message


# --- unexpected files are errors, never silently ignored (SP1 review, M1-74) ---


def test_yml_policy_file_rejected(policy_dir: Path):
    (policy_dir / "tools.yaml").rename(policy_dir / "tools.yml")  # would drop all forbidden tools

    with pytest.raises(PolicyError) as error:
        load_policy(policy_dir)

    assert "tools.yml" in str(error.value)


def test_yml_team_file_rejected(policy_dir: Path):
    (policy_dir / "teams" / "payments.yaml").rename(policy_dir / "teams" / "payments.yml")

    with pytest.raises(PolicyError) as error:
        load_policy(policy_dir)

    assert "payments.yml" in str(error.value)


def test_other_files_in_policy_folder_rejected(policy_dir: Path):
    (policy_dir / "notes.txt").write_text("todo", encoding="utf-8")

    with pytest.raises(PolicyError) as error:
        load_policy(policy_dir)

    assert "notes.txt" in str(error.value)


def test_hidden_files_are_ignored(policy_dir: Path):
    (policy_dir / ".gitkeep").write_text("", encoding="utf-8")

    assert load_policy(policy_dir).default_classification == "internal"


def test_duplicate_key_in_policy_file_rejected(policy_dir: Path):
    tools = policy_dir / "tools.yaml"
    tools.write_text(tools.read_text(encoding="utf-8") + "forbidden: []\n", encoding="utf-8")

    with pytest.raises(PolicyError) as error:
        load_policy(policy_dir)

    assert "duplicate" in str(error.value).lower()
