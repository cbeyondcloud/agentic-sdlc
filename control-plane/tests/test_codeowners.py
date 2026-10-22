"""Repo maintainers from CODEOWNERS (SP15 Task 8, ND-6): the owners of the agentic paths,
last match wins, `@org/team` reported unresolved; the file is fetched over git."""

from __future__ import annotations

from policy_repos import commit, git, make_app_repo, url, write

from agentic_cp.gitops import GitRunner
from agentic_cp.rbac.codeowners import PROBE_PATHS, fetch_codeowners, maintainers
from agentic_cp.settings import Settings


def test_codeowners_maintainers_union_of_agentic_paths():
    text = "* @carol\n/.agentic/ @Alice\nAGENTS.md bob@x.com\n"
    found = maintainers(text, "github.com")
    assert found.handles == {"github.com:alice"}
    assert found.emails == {"bob@x.com"}
    assert found.unresolved == set()
    assert "github.com:carol" not in found.handles  # overridden on every probe path


def test_codeowners_team_entries_reported_unresolved():
    found = maintainers("/.agentic/ @acme/platform @dave\n", "ghe.acme.test")
    assert found.unresolved == {"@acme/platform"}
    assert found.handles == {"ghe.acme.test:dave"}


def test_codeowners_without_agentic_owners_has_no_maintainers():
    assert maintainers("/src/ @erin\n", "github.com").handles == set()
    assert "AGENTS.md" in PROBE_PATHS and ".agentic/config.yaml" in PROBE_PATHS


def test_codeowners_entries_name_their_line():
    found = maintainers("* @carol\n/.agentic/ @alice\n", "github.com")
    assert found.entries["github.com:alice"] == "line 2: /.agentic/"


def _settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "AGENTIC_CP_DATABASE_URL": "postgresql://x@127.0.0.1:9/none",
            "AGENTIC_CP_ALLOW_LOCAL_REPOS": "true",
            "AGENTIC_CP_POLICY_WORKDIR": str(tmp_path / "work"),
        }
    )


def test_fetch_codeowners_from_git_default_branch(tmp_path):
    repo = make_app_repo(tmp_path / "app", "/.agentic/ @alice\n")
    git(repo, "switch", "-q", "-c", "feature")
    write(repo, ".github/CODEOWNERS", "/.agentic/ @mallory\n")
    commit(repo, "feature branch owners")
    git(repo, "switch", "-q", "main")
    found = fetch_codeowners(GitRunner(_settings(tmp_path)), tmp_path / "work", url(repo), None)
    assert found is not None
    assert found.path == ".github/CODEOWNERS"
    assert found.text == "/.agentic/ @alice\n"
    assert len(found.commit) == 40


def test_fetch_codeowners_takes_the_first_location(tmp_path):
    repo = make_app_repo(tmp_path / "app", "* @root\n", where="CODEOWNERS")
    write(repo, "docs/CODEOWNERS", "* @docs\n")
    commit(repo, "two files")
    found = fetch_codeowners(GitRunner(_settings(tmp_path)), tmp_path / "work", url(repo), None)
    assert found.path == "CODEOWNERS"


def test_fetch_codeowners_without_a_file_is_none(tmp_path):
    repo = make_app_repo(tmp_path / "app", "x\n", where="notes.txt")
    assert fetch_codeowners(GitRunner(_settings(tmp_path)), tmp_path / "w", url(repo), None) is None
