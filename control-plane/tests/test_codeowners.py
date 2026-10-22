"""Repo maintainers from CODEOWNERS (SP15 Task 8, ND-6): the owners of the agentic paths,
last match wins, `@org/team` reported unresolved; the file is fetched over git."""

from __future__ import annotations

import os
import time

from policy_repos import commit, git, make_app_repo, url, write

from agentic_cp.gitops import GitRunner
from agentic_cp.rbac.codeowners import (
    MAX_CODEOWNERS_BYTES,
    PROBE_PATHS,
    fetch_codeowners,
    maintainers,
)
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


def test_a_run_of_stars_is_matched_quickly_and_means_one_star():
    """Review F3: stacked '*' made the library's regex backtrack for seconds per line on
    the shared event loop; a run of them means what one '*' means, and is matched so."""
    started = time.perf_counter()
    found = maintainers("*" * 22 + ".md @alice\n" + "*" * 22 + "b @bob\n", "github.com")
    assert time.perf_counter() - started < 1
    assert found.handles == {"github.com:alice"}


def test_a_pattern_with_too_many_wildcards_derives_nobody():
    found = maintainers("* @carol\n/" + "*a" * 9 + " @alice\n", "github.com")
    assert found.owners == set()
    assert any("wildcards" in problem for problem in found.unresolved)


def test_an_oversized_codeowners_derives_nobody():
    """GitHub ignores a CODEOWNERS file over 3 MiB; so does the control plane."""
    text = "/.agentic/ @alice\n" + "# padding\n" * (MAX_CODEOWNERS_BYTES // 10 + 1)
    found = maintainers(text, "github.com")
    assert found.owners == set()
    assert any("3 MiB" in problem for problem in found.unresolved)


def _settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "AGENTIC_CP_DATABASE_URL": "postgresql://x@127.0.0.1:9/none",
            "AGENTIC_CP_ALLOW_LOCAL_REPOS": "true",
            "AGENTIC_CP_POLICY_WORKDIR": str(tmp_path / "work"),
        }
    )


def test_git_env_holds_no_server_credentials(tmp_path, monkeypatch):
    """R25: without its own gitconfig a tenant's git sees no system or global config, no
    ssh key and no inherited GIT_SSH_COMMAND; with one, only that config."""
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /srv/keys/deploy")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'credential.helper=store'")
    monkeypatch.setenv("GIT_SSL_CAINFO", "/etc/ssl/corp.pem")
    config_dir = tmp_path / "gitconfig"
    config_dir.mkdir()
    (config_dir / "tenant-a.gitconfig").write_text("[core]\n", encoding="utf-8")
    settings = _settings(tmp_path).model_copy(update={"git_config_dir": config_dir})
    bare = GitRunner(settings).env("tenant-b")
    assert bare["GIT_CONFIG_GLOBAL"] == os.devnull and bare["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "/srv/keys/deploy" not in bare["GIT_SSH_COMMAND"]
    assert "IdentityFile=none" in bare["GIT_SSH_COMMAND"]
    assert "GIT_CONFIG_PARAMETERS" not in bare and bare["GIT_SSL_CAINFO"] == "/etc/ssl/corp.pem"
    own = GitRunner(settings).env("tenant-a")
    assert own["GIT_CONFIG_GLOBAL"] == str(config_dir / "tenant-a.gitconfig")
    assert "GIT_SSH_COMMAND" not in own and own["GIT_CONFIG_NOSYSTEM"] == "1"


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
