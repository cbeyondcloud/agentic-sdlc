"""Tests for the sandbox rules and their `srt` settings file (M1-32; spike S6: srt refuses
a settings file that lacks a key, so every key is always written)."""

from pathlib import Path

import pytest

from agentic.sandbox import SandboxError, SandboxRules, expand_home, srt_settings


def test_settings_always_carry_every_key(tmp_path):
    s = srt_settings(SandboxRules(write=(tmp_path,)))
    assert set(s) == {"network", "filesystem"}
    assert set(s["network"]) == {"allowedDomains", "deniedDomains"}
    assert set(s["filesystem"]) == {"denyRead", "allowRead", "allowWrite", "denyWrite"}


def test_settings_carry_each_rule(tmp_path):
    rules = SandboxRules(
        write=(tmp_path / "tree",),
        deny_write=(tmp_path / "tree" / ".env",),
        deny_read=(tmp_path / ".ssh",),
        allow_read=(tmp_path / "tree",),
        allowed_domains=("pypi.org",),
    )
    s = srt_settings(rules)
    assert s["network"] == {"allowedDomains": ["pypi.org"], "deniedDomains": []}
    assert s["filesystem"]["allowWrite"] == [(tmp_path / "tree").as_posix()]
    assert s["filesystem"]["denyWrite"] == [(tmp_path / "tree" / ".env").as_posix()]
    assert s["filesystem"]["denyRead"] == [(tmp_path / ".ssh").as_posix()]
    assert s["filesystem"]["allowRead"] == [(tmp_path / "tree").as_posix()]


def test_paths_with_spaces_survive(tmp_path):
    d = tmp_path / "my repo"
    assert srt_settings(SandboxRules(write=(d,)))["filesystem"]["allowWrite"] == [d.as_posix()]


def test_no_writable_path_is_refused():
    with pytest.raises(SandboxError, match="writable"):
        srt_settings(SandboxRules(write=()))


def test_a_relative_path_is_refused(tmp_path):
    with pytest.raises(SandboxError, match="absolute"):
        srt_settings(SandboxRules(write=(Path("tree"),)))


def test_expand_home(tmp_path):
    absolute = tmp_path / "etc" / "x"
    assert expand_home(["~/.ssh", "~", str(absolute)], tmp_path) == (
        tmp_path / ".ssh",
        tmp_path,
        absolute,
    )
