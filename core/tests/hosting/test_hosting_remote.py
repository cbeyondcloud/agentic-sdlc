"""Which repository a git remote names (M1-58): host, owner and name, never credentials."""

import pytest

from agentic.hosting import HostingError, RepoRef, parse_remote


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/app.git",
        "https://github.com/acme/app",
        "https://github.com/acme/app/",
        "http://github.com/acme/app.git",
        "ssh://git@github.com/acme/app.git",
        "ssh://git@github.com:22/acme/app.git",
        "git@github.com:acme/app.git",
        "GitHub.com:acme/app",
    ],
)
def test_every_remote_form_names_the_same_repository(url):
    assert parse_remote(url) == RepoRef("github.com", "acme", "app")


def test_userinfo_is_dropped_and_never_kept():
    secret = "x-access-token:" + "abc" + "123"
    ref = parse_remote(f"https://{secret}@ghe.example.com/team/svc.git")
    assert ref == RepoRef("ghe.example.com", "team", "svc")
    assert "abc123" not in repr(ref)


def test_a_port_is_not_part_of_the_host():
    assert parse_remote("https://ghe.example.com:8443/acme/app.git").host == "ghe.example.com"


def test_nested_owners_are_kept_whole():
    ref = parse_remote("https://git.example.com/group/sub/app.git")
    assert (ref.owner, ref.name, ref.slug) == ("group/sub", "app", "group/sub/app")


@pytest.mark.parametrize(
    "url",
    ["/srv/git/app.git", "C:\\repos\\app.git", "file:///srv/git/app.git", "../app", "", "x"],
)
def test_a_local_path_is_not_a_hosted_repository(url):
    with pytest.raises(HostingError):
        parse_remote(url)


def test_a_url_without_an_owner_is_refused():
    with pytest.raises(HostingError, match="owner"):
        parse_remote("https://github.com/app.git")
