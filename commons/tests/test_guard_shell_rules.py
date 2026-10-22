"""The guard's shell policy in the provider-neutral form ACP profiles compile (M2-57): the
agent's lists plus the framework floor and every protected path."""

import subprocess
from pathlib import Path

from agentic.backends.acp import ShellRules
from sdlc_commons.guards import FRAMEWORK_DENY_SHELL, FRAMEWORK_PROTECTED, Guard

RUN = "r-rules-1"


def test_shell_rules_carry_the_framework_floor(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    rules = ShellRules(
        **Guard(
            repo,
            RUN,
            allow_shell=["pytest"],
            ask_shell=["npm publish"],
            deny_shell=["rm -rf"],
            deny_paths=["infra/**"],
        ).shell_rules()
    )
    assert rules.allow[0] == "pytest" and "cd" in rules.allow  # shell builtins too
    assert rules.ask == ("npm publish",)
    assert rules.deny[0] == "rm -rf"
    assert set(FRAMEWORK_DENY_SHELL) <= set(rules.deny)
    assert "infra/**" in rules.deny_paths
    assert set(FRAMEWORK_PROTECTED) <= set(rules.deny_paths)
    assert len(rules.deny) == len(set(rules.deny))


def test_without_an_allow_list_everything_not_denied_is_allowed(tmp_path: Path):
    rules = Guard(tmp_path, RUN, allow_shell=None).shell_rules()
    assert rules["allow"] == ("*",)
    assert "git push" in rules["deny"]
