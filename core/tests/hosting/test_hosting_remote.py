"""Which repository a git remote names (M1-58): host, owner and name, never credentials."""

import re
import time
from pathlib import Path

import pytest

from agentic.hosting import HostingError, RepoRef, parse_remote
from agentic.hosting import base as hosting_base
from agentic.hosting.base import PR_BODY_END, PR_BODY_START, scrub_url_credentials, splice_body


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


# ── M1 close-out (DF-297, DF-296, DF-300) ────────────────────────────────────

PASSWORD = "p@ss" + "w0rd"  # built from pieces; holds an unencoded '@'


def test_a_password_with_an_at_sign_is_scrubbed_whole():  # DF-297
    text = f"fatal: unable to access 'https://dev:{PASSWORD}@git.example.com/acme/app.git/'"
    scrubbed = scrub_url_credentials(text)
    assert scrubbed == "fatal: unable to access 'https://git.example.com/acme/app.git/'"


def test_an_at_sign_after_the_host_is_not_userinfo():
    text = "see https://git.example.com/acme/app@v2 and https://h.example/x?mail=a@b"
    assert scrub_url_credentials(text) == text


def test_scrubbing_a_long_value_is_linear():  # the scheme rule must not backtrack
    started = time.monotonic()
    scrub_url_credentials("a" * 20_000 + "://")
    scrub_url_credentials("a://" * 5_000)
    assert time.monotonic() - started < 0.25


BODY = f"{PR_BODY_START}\n## Agentic run `r-1`\n- Status: completed\n{PR_BODY_END}\n"


def test_an_update_replaces_only_the_generated_part():  # DF-296
    current = (
        "Reviewer note: check the migration first.\n\n"
        f"{PR_BODY_START}\n## Agentic run `r-1`\n- Status: running\n{PR_BODY_END}\n\n"
        "Follow-up: ticket PAY-12."
    )
    spliced = splice_body(current, BODY)
    assert spliced.startswith("Reviewer note: check the migration first.\n\n")
    assert spliced.endswith(f"{PR_BODY_END}\n\nFollow-up: ticket PAY-12.")
    assert "- Status: completed" in spliced and "- Status: running" not in spliced


def test_the_generated_part_runs_to_its_last_end_marker():  # review F2
    """The generated text is ours: an end marker inside it (agent text the body failed to
    neutralise) must not cut the new part short."""
    generated = f"{PR_BODY_START}\nnew {PR_BODY_END} inside\nnew tail\n{PR_BODY_END}\n"
    current = f"mine\n{PR_BODY_START}\nold\n{PR_BODY_END}\ntheirs"

    spliced = splice_body(current, generated)

    assert spliced == "mine\n" + generated.removesuffix("\n") + "\ntheirs"


@pytest.mark.parametrize("current", [None, "", "an old description with no markers"])
def test_a_description_without_markers_is_replaced_whole(current):
    assert splice_body(current, BODY) == BODY


def test_the_hosting_protocol_names_no_vendor():  # DF-300
    text = (Path(hosting_base.__file__)).read_text(encoding="utf-8")
    assert not re.search(r"github|gitlab|azure|bitbucket", text, re.IGNORECASE)
