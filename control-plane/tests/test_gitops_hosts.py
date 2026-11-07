"""`check_git_host` (DF-484, DF-503; owner decision 2026-11-07): the control plane fetches
git only from a host the org policy allows, and a host that is not listed by name must
resolve to public addresses only - checked again at every fetch, since DNS can change."""

from __future__ import annotations

import pytest

from agentic_cp.gitops import GitHosts, RepoUrlError, blocked_address, check_git_host, url_host

PUBLIC = ["140.82.112.3"]


def resolver(*addresses: str):
    calls: list[str] = []

    def resolve(host: str) -> list[str]:
        calls.append(host)
        return list(addresses)

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://GitHub.com/acme/app.git", "github.com"),
        ("https://ghe.acme.example:8443/acme/app.git", "ghe.acme.example"),
        ("ssh://git@ghe.acme.example:2222/acme/app.git", "ghe.acme.example"),
        ("git@ghe.acme.example:acme/app.git", "ghe.acme.example"),
        ("https://[::1]/acme/app.git", "::1"),
        ("https://10.0.0.5/acme/app.git", "10.0.0.5"),
        ("file:///srv/git/app.git", None),
        ("/srv/git/app.git", None),
    ],
)
def test_url_host(url, host):
    assert url_host(url) == host


@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",
        "224.0.0.1",
        "100.64.0.1",  # shared address space
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4-mapped IPv6
        "::ffff:169.254.169.254",
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "2002:a00:1::1",  # 6to4 of 10.0.0.1
    ],
)
def test_internal_addresses_are_blocked(address):
    assert blocked_address(address)


@pytest.mark.parametrize("address", ["140.82.112.3", "2606:50c0:8000::153", "::ffff:8.8.8.8"])
def test_public_addresses_are_not_blocked(address):
    assert not blocked_address(address)


def test_an_unlisted_host_is_refused_without_resolving_it():
    resolve = resolver(*PUBLIC)
    hosts = GitHosts(explicit=frozenset({"github.com"}))
    with pytest.raises(RepoUrlError, match="allowed_git_hosts"):
        check_git_host("https://internal.acme.example/x/y.git", hosts, resolve=resolve)
    assert resolve.calls == []


def test_an_explicit_host_is_allowed_whatever_it_resolves_to():
    """An org's own GHE host on a private network is listed by name: it is not resolved."""
    resolve = resolver("10.0.0.7")
    hosts = GitHosts(explicit=frozenset({"ghe.acme.example"}))
    check_git_host("git@GHE.acme.example:acme/app.git", hosts, resolve=resolve)
    assert resolve.calls == []


@pytest.mark.parametrize(
    "answer", [["10.0.0.7"], ["140.82.112.3", "127.0.0.1"], ["::ffff:10.0.0.1"]]
)
def test_an_implicit_host_must_resolve_to_public_addresses_only(answer):
    hosts = GitHosts(implicit=frozenset({"github.com"}))
    with pytest.raises(RepoUrlError) as refused:
        check_git_host("https://github.com/acme/app.git", hosts, resolve=resolver(*answer))
    assert "10.0.0" not in str(refused.value) and "127.0" not in str(refused.value)


def test_an_implicit_host_on_public_addresses_is_allowed():
    hosts = GitHosts(implicit=frozenset({"github.com"}))
    check_git_host("https://github.com/acme/app.git", hosts, resolve=resolver(*PUBLIC))


def test_an_unresolvable_implicit_host_is_refused():
    def fails(host: str) -> list[str]:
        raise OSError("no such host")

    hosts = GitHosts(implicit=frozenset({"github.com"}))
    with pytest.raises(RepoUrlError):
        check_git_host("https://github.com/acme/app.git", hosts, resolve=fails)
    with pytest.raises(RepoUrlError):
        check_git_host("https://github.com/acme/app.git", hosts, resolve=resolver())


def test_an_ip_literal_must_be_listed_too():
    hosts = GitHosts(implicit=frozenset({"github.com"}))
    for url in ("https://169.254.169.254/x/y.git", "https://[::ffff:7f00:1]/x/y.git"):
        with pytest.raises(RepoUrlError):
            check_git_host(url, hosts, resolve=resolver(*PUBLIC))
    listed = GitHosts(explicit=frozenset({"10.0.0.5"}))
    check_git_host("https://10.0.0.5/x/y.git", listed, resolve=resolver())


def test_a_local_repo_has_no_host_to_check():
    check_git_host("file:///srv/git/app.git", GitHosts(), resolve=resolver())
