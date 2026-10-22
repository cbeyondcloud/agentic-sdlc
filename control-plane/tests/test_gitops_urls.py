"""`check_repo_url` (R12, review F1): a tenant-set repository URL is refused unless git would
fetch it from the very host its registration key and the allow-list see."""

from __future__ import annotations

import pytest

from agentic.policy.repos import normalise_remote
from agentic_cp.gitops import RepoUrlError, check_repo_url


@pytest.mark.parametrize(
    "url",
    [
        "https://git.attacker.test#@git.example.test/acme/app.git",
        "https://git.attacker.test?@git.example.test/acme/app.git",
        "https://127.0.0.1:1#@git.example.test/acme/app",
        "ssh://git@git.attacker.test#@git.example.test/acme/app.git",
        "https://git.example.test/acme/app.git#frag",
        "https://git.example.test/acme/app.git?x=1",
        "https://git.example.test\\@git.attacker.test/acme/app",
        "https://git.example.test/acme/a@pp.git",
        "ssh://git@host@git.example.test/acme/app.git",
        "ssh://-oProxyCommand=x@git.example.test/acme/app.git",
        "git@git.attacker.test:x@git.example.test/acme/app.git",
        "git@git.attacker.test:x#y",
    ],
)
def test_confusable_urls_are_refused(url):
    with pytest.raises(RepoUrlError):
        check_repo_url(url, allow_local=False)


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://git.example.test/acme/app.git", "git.example.test"),
        ("https://git.example.test:8443/acme/app.git", "git.example.test"),
        ("ssh://git@git.example.test/acme/app.git", "git.example.test"),
        ("ssh://git@git.example.test:2222/acme/app.git", "git.example.test"),
        ("git@git.example.test:acme/app.git", "git.example.test"),
        ("git@git.example.test:/srv/git/policy.git", "git.example.test"),
    ],
)
def test_accepted_urls_key_on_the_host_git_fetches_from(url, host):
    assert check_repo_url(url, allow_local=False) == url
    assert normalise_remote(url).split("/", 1)[0] == host
