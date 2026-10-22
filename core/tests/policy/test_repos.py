"""`agentic.policy.repos` (SP15 Task 8): one repo matcher for the CLI guard (check 5) and the
control plane (repo registration), so both decide `allowed_repo_patterns` alike."""

import pytest

from agentic.policy.repos import normalise_remote, repo_allowed, repo_host

#: The cases `cli/tests/test_repo_guard.py` holds for the CLI guard (DF-199 included).
REMOTES = [
    ("https://github.com/Acme/App.git", "github.com/acme/app"),
    ("git@github.com:acme/app.git", "github.com/acme/app"),
    ("ssh://git@github.com/acme/app", "github.com/acme/app"),
    ("https://user@github.com/acme/app/", "github.com/acme/app"),
    ("git@github.com:123/app.git", "github.com/123/app"),
    ("https://github.com/Acme/App.GIT", "github.com/acme/app"),
    ("https://ghe.acme.test:8443/acme/app.git", "ghe.acme.test/acme/app"),
    ("ssh://git@ghe.acme.test:2222/acme/app.git", "ghe.acme.test/acme/app"),
]


@pytest.mark.parametrize(("url", "expected"), REMOTES)
def test_normalise_remote_matches_the_cli_guard(url, expected):
    assert normalise_remote(url) == expected


def test_repo_allowed_matches_cli_guard():
    patterns = ["github.com/acme/*", "git@ghe.acme.test:platform/tools.git"]
    assert repo_allowed(patterns, "git@github.com:acme/app.git")
    assert repo_allowed(patterns, "https://GitHub.com/Acme/Other")
    assert repo_allowed(patterns, "ssh://git@ghe.acme.test:2222/platform/tools.git")
    assert not repo_allowed(patterns, "git@github.com:someone-else/app.git")
    assert not repo_allowed(patterns, "https://ghe.acme.test/platform/other.git")
    assert not repo_allowed([], "git@github.com:acme/app.git")  # empty allows nothing


@pytest.mark.parametrize(
    "url",
    [
        "https://git.attacker.test#@git.example.test/acme/app.git",
        "https://git.attacker.test?@git.example.test/acme/app.git",
        "ssh://git.attacker.test#@git.example.test/acme/app.git",
    ],
)
def test_the_key_names_the_host_git_would_fetch_from(url):
    """Review F1: an '@' after '#' or '?' is not userinfo - git (like urlsplit) fetches from
    the host before it, so the key and the allow-list must see that host too."""
    assert normalise_remote(url).startswith("git.attacker.test")
    assert not repo_allowed(["git.example.test/acme/*"], url)


def test_repo_host_is_the_first_segment():
    assert repo_host("github.com/acme/app") == "github.com"
