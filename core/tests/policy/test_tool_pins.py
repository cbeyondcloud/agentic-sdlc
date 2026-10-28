"""Tests for the org's tool pins (`tool-pins.yaml`, M1-51) and the index export switch."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, ToolPin, load_policy

HASH = "a" * 64


@pytest.fixture
def policy_dir(fixtures: Path, tmp_path: Path) -> Path:
    target = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", target)
    return target


def _pins(folder: Path, body: str) -> None:
    (folder / "tool-pins.yaml").write_text(body, encoding="utf-8")


def test_pins_load_per_platform(policy_dir: Path):
    _pins(
        policy_dir,
        f"""tools:
  gitleaks:
    version: 8.28.0
    binary: gitleaks
    assets:
      linux-x86_64: {{file: gitleaks/8.28.0/gitleaks_linux_x64.tar.gz, sha256: "{HASH}"}}
      windows-x86_64:
        file: gitleaks/8.28.0/gitleaks_windows_x64.zip
        sha256: "{HASH}"
        signed: true
""",
    )
    policy = load_policy(policy_dir)
    pin = policy.tool_pins["gitleaks"]
    assert isinstance(pin, ToolPin) and pin.when == "always"
    assert pin.assets["linux-x86_64"].signed is False
    assert pin.assets["windows-x86_64"].signed is True


def test_no_pins_by_default(policy_dir: Path):
    policy = load_policy(policy_dir)
    assert policy.tool_pins == {} and policy.index_export_workflow is False


@pytest.mark.parametrize(
    "file",
    ["https://evil.example/x.zip", "/abs/x.zip", "../x.zip", "a/../../x.zip", "a\\..\\x.zip"],
)
def test_an_artefact_path_stays_under_the_mirror(policy_dir: Path, file: str):
    _pins(
        policy_dir,
        f"""tools:
  t: {{version: "1", binary: t, assets: {{linux-x86_64: {{file: '{file}', sha256: "{HASH}"}}}}}}
""",
    )
    with pytest.raises(PolicyError, match="mirror"):
        load_policy(policy_dir)


def test_a_bad_hash_or_platform_is_refused(policy_dir: Path):
    _pins(
        policy_dir,
        """tools:
  t: {version: "1", binary: t, assets: {beos-x86: {file: t.zip, sha256: "nothex"}}}
""",
    )
    with pytest.raises(PolicyError) as raised:
        load_policy(policy_dir)
    assert "beos-x86" in str(raised.value) and "sha256" in str(raised.value)


def test_pins_need_a_mirror(policy_dir: Path):
    org = policy_dir / "org.yaml"
    org.write_text(
        org.read_text(encoding="utf-8").replace("tool_mirror_url:", "# tool_mirror_url:"),
        encoding="utf-8",
    )
    _pins(
        policy_dir,
        f"""tools:
  t: {{version: "1", binary: t, assets: {{linux-x86_64: {{file: t.zip, sha256: "{HASH}"}}}}}}
""",
    )
    with pytest.raises(PolicyError, match="tool_mirror_url"):
        load_policy(policy_dir)


def test_a_binary_is_a_base_name(policy_dir: Path):
    _pins(
        policy_dir,
        f"""tools:
  t: {{version: "1", binary: t.exe, assets: {{linux-x86_64: {{file: t.zip, sha256: "{HASH}"}}}}}}
""",
    )
    with pytest.raises(PolicyError, match="base name"):
        load_policy(policy_dir)


def test_index_export_workflow_is_an_org_setting(policy_dir: Path):
    org = policy_dir / "org.yaml"
    org.write_text(
        org.read_text(encoding="utf-8") + "index_export_workflow: true\n", encoding="utf-8"
    )
    assert load_policy(policy_dir).index_export_workflow is True


@pytest.mark.parametrize(
    ("name", "version", "binary"),
    [
        ("'..'", "'1'", "t"),
        ("'.'", "'1'", "t"),
        ("'a/b'", "'1'", "t"),
        ("'C:'", "'1'", "t"),
        ("t", "'..'", "t"),
        ("t", "'1/../..'", "t"),
        ("t", "'/abs'", "t"),
        ("t", "'1'", "'..'"),
        ("t", "'1'", "'.hidden'"),
        ("t", "'1'", "'a:b'"),
    ],
)
def test_names_versions_and_binaries_are_plain_path_parts(
    policy_dir: Path, name: str, version: str, binary: str
):
    """The tool name, version and binary become cache folder names: never '..' or a path."""
    _pins(
        policy_dir,
        f"""tools:
  {name}:
    version: {version}
    binary: {binary}
    assets: {{linux-x86_64: {{file: t.zip, sha256: "{HASH}"}}}}
""",
    )
    with pytest.raises(PolicyError, match="plain name"):
        load_policy(policy_dir)
