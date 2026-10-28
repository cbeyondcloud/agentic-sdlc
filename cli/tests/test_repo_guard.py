"""The eight repo-root and safety checks before `init` writes anything (M1-50, spec §24).
One test per check, plus the path cases: a subfolder, a junction or symlink to the root,
and a case-changed path on Windows. Real git, temporary repos."""

import os
from pathlib import Path

import pytest
from cli_helpers import FIXTURES, commit_file, git, link_dir, make_repo

from agentic.policy import OrgPolicy, load_policy
from agentic_cli.exit_codes import ExitCode
from agentic_cli.repo_guard import GuardRefusal, check_repo, normalise_remote

CONFIG = "version: 1\nclassification: internal\n"


@pytest.fixture
def policy() -> OrgPolicy:
    return load_policy(FIXTURES / "policy" / "policy")  # allows github.com/acme/*


def refused(check: int, cwd: Path, **kwargs) -> GuardRefusal:
    kwargs.setdefault("mode", "repo")
    kwargs.setdefault("home", Path(cwd.anchor) / "no-such-home")
    with pytest.raises(GuardRefusal) as raised:
        check_repo(cwd, **kwargs)
    assert raised.value.code == ExitCode.REFUSED
    assert raised.value.check == check, str(raised.value)
    return raised.value


def ok(cwd: Path, **kwargs):
    kwargs.setdefault("mode", "repo")
    kwargs.setdefault("home", Path(cwd.anchor) / "no-such-home")
    return check_repo(cwd, **kwargs)


# --- 1: inside a git working tree, not bare ---


def test_1_outside_a_git_repo(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "Not a git repository" in str(refused(1, plain))


def test_1_a_bare_repo(tmp_path: Path):
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    refused(1, bare)


# --- 2: the current directory is the repo root ---


def test_2_a_subfolder_is_refused_with_the_root_named(repo: Path):
    error = refused(2, repo / "src")
    assert "cd " in (error.hint or "") and repo.name in (error.hint or "")


def test_2_the_root_is_accepted(repo: Path):
    assert ok(repo).path.samefile(repo)


def test_2_a_link_to_the_root_is_accepted(repo: Path, tmp_path: Path):
    link = tmp_path / "linked-app"
    link_dir(link, repo)
    assert ok(link).path.samefile(repo)


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_2_a_case_changed_path_is_accepted_on_windows(repo: Path):
    assert ok(Path(str(repo).upper())).path.samefile(repo)


# --- 3: not a run worktree, not a submodule ---


def test_3_an_agentic_run_worktree_is_refused(repo: Path):
    tree = repo / ".agentic" / "local" / "worktrees" / "r-1"
    git(repo, "worktree", "add", "-q", "-b", "run/r-1", str(tree))
    assert "worktree" in str(refused(3, tree))


def test_3_a_submodule_is_refused(tmp_path: Path):
    child = make_repo(tmp_path / "child", origin=None)
    parent = make_repo(tmp_path / "parent")
    git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "libs/c")
    git(parent, "commit", "-qm", "submodule")
    assert "submodule" in str(refused(3, parent / "libs" / "c"))


def test_3_a_submodule_listed_as_a_component_is_accepted(tmp_path: Path):
    child = make_repo(tmp_path / "child", origin="https://github.com/acme/child.git")
    parent = make_repo(tmp_path / "parent")
    git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "libs/c")
    commit_file(parent, ".agentic/config.yaml", CONFIG + "components: [libs/c]\n")
    ok(parent / "libs" / "c")


# --- 4: not a dangerous location ---


def test_4_the_home_folder_is_refused_even_as_a_repo(tmp_path: Path):
    user_home = make_repo(tmp_path / "me")
    assert "Refusing to initialise" in str(refused(4, user_home, home=user_home))


def test_4_a_filesystem_root_is_refused(monkeypatch: pytest.MonkeyPatch, repo: Path):
    import agentic_cli.repo_guard as guard

    monkeypatch.setattr(guard, "_is_root", lambda path: True)
    refused(4, repo)


# --- 5: repo allowed by org policy (org mode) ---


def test_5_a_repo_outside_the_allowed_patterns(tmp_path: Path, policy: OrgPolicy):
    other = make_repo(tmp_path / "other", origin="git@github.com:someone-else/app.git")
    error = refused(5, other, org_mode=True, policy=policy)
    assert "not approved" in str(error)


def test_5_an_allowed_repo_passes(repo: Path, policy: OrgPolicy):
    ok(repo, org_mode=True, policy=policy, permitted=lambda permission: True)


def test_5_no_origin_is_refused_in_org_mode(tmp_path: Path, policy: OrgPolicy):
    refused(5, make_repo(tmp_path / "local", origin=None), org_mode=True, policy=policy)


def test_5_an_empty_allow_list_refuses_in_org_mode(repo: Path, policy: OrgPolicy):
    empty = policy.model_copy(update={"allowed_repo_patterns": []})
    refused(5, repo, org_mode=True, policy=empty)


def test_5_is_skipped_standalone(tmp_path: Path, policy: OrgPolicy):
    ok(make_repo(tmp_path / "local", origin=None), policy=policy)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Acme/App.git",
        "git@github.com:acme/app.git",
        "ssh://git@github.com/acme/app",
        "https://user@github.com/acme/app/",
    ],
)
def test_remote_urls_normalise_to_host_owner_repo(url: str):
    assert normalise_remote(url) == "github.com/acme/app"


# --- 6: developer init needs the committed config with a known classification ---


def test_6_developer_init_before_init_repo(repo: Path, policy: OrgPolicy):
    error = refused(6, repo, mode="developer", policy=policy)
    assert "agentic init --repo" in str(error)


def test_6_a_standalone_developer_is_told_the_standalone_command(repo: Path, policy: OrgPolicy):
    """A person trying agentic alone owns the repo setup too: `init --repo` alone would
    look for an org (DF-284)."""
    error = refused(6, repo, mode="developer", policy=policy, org_mode=False)
    assert "agentic init --repo --standalone" in str(error)
    error = refused(6, repo, mode="developer", policy=policy, org_mode=True)
    assert "agentic init --repo" in str(error) and "--standalone" not in str(error)


def test_6_an_uncommitted_config_does_not_count(repo: Path, policy: OrgPolicy):
    (repo / ".agentic").mkdir()
    (repo / ".agentic" / "config.yaml").write_text(CONFIG, encoding="utf-8")
    refused(6, repo, mode="developer", policy=policy)


def test_6_an_unknown_classification(repo: Path, policy: OrgPolicy):
    commit_file(repo, ".agentic/config.yaml", "version: 1\nclassification: top-secret\n")
    assert "top-secret" in str(refused(6, repo, mode="developer", policy=policy))


def test_6_developer_init_on_a_set_up_repo(repo: Path, policy: OrgPolicy):
    commit_file(repo, ".agentic/config.yaml", CONFIG)
    root = ok(repo, mode="developer", policy=policy)
    assert root.config is not None and root.config.classification == "internal"


def test_6_init_repo_on_a_set_up_repo(repo: Path, policy: OrgPolicy):
    commit_file(repo, ".agentic/config.yaml", CONFIG)
    assert "already" in str(refused(6, repo, policy=policy))


# --- 7: init --repo needs the permission, a clean tree and the default branch ---


def test_7_uncommitted_changes(repo: Path):
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    assert "uncommitted" in str(refused(7, repo))


def test_7_not_on_the_default_branch(repo: Path):
    git(repo, "switch", "-q", "-c", "feature")
    assert "main" in str(refused(7, repo))


def test_7_without_the_permission(repo: Path, policy: OrgPolicy):
    error = refused(7, repo, org_mode=True, policy=policy, permitted=lambda permission: False)
    assert "agent.edit.repo" in str(error)


def test_7_org_mode_without_a_permission_check_is_refused(repo: Path, policy: OrgPolicy):
    refused(7, repo, org_mode=True, policy=policy)


# --- 8: no nested setup ---


def test_8_another_agentic_config_below_the_root(repo: Path, policy: OrgPolicy):
    commit_file(repo, ".agentic/config.yaml", CONFIG)
    commit_file(repo, "services/api/.agentic/config.yaml", CONFIG)
    error = refused(8, repo, mode="developer", policy=policy)
    assert "services/api/.agentic" in str(error)


def test_8_an_untracked_nested_config_is_found_too(repo: Path):
    nested = repo / "pkg" / ".agentic"
    nested.mkdir(parents=True)
    (nested / "config.yaml").write_text(CONFIG, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "nested")
    refused(8, repo)


# --- M1 close-out (DF-195, DF-197..DF-200, DF-205, DF-211) ---


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:123/app.git", "github.com/123/app"),  # numeric owner, not a port
        ("https://github.com/Acme/App.GIT", "github.com/acme/app"),
        ("https://ghe.acme.test:8443/acme/app.git", "ghe.acme.test/acme/app"),
        ("ssh://git@ghe.acme.test:2222/acme/app.git", "ghe.acme.test/acme/app"),
    ],
)
def test_remote_url_edge_cases_normalise_correctly(url: str, expected: str):
    """DF-199: the scp form has no port; `.GIT` is stripped whatever its case."""
    assert normalise_remote(url) == expected


def test_5_an_empty_allow_list_names_the_setting_an_admin_sets(repo: Path, policy: OrgPolicy):
    """DF-211: a team lead cannot fix an empty allow-list; the org admin's setting is named."""
    empty = policy.model_copy(update={"allowed_repo_patterns": []})
    error = refused(5, repo, org_mode=True, policy=empty)
    assert "allowed_repo_patterns" in (error.hint or "")


def test_3_a_linked_worktree_nested_in_its_main_tree_is_refused(repo: Path):
    """DF-200: a hand-made worktree inside the main checkout would put a second .agentic
    inside the first."""
    nested = repo / "wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(nested))
    assert "inside" in str(refused(3, nested))


def test_3_a_linked_worktree_beside_the_main_tree_is_accepted(repo: Path, tmp_path: Path):
    """Developers keep worktrees next to the main checkout: that stays allowed."""
    beside = tmp_path / "work" / "app-side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(beside))
    git(beside, "switch", "-q", "main", "--ignore-other-worktrees")
    assert ok(beside).path.samefile(beside)


def test_6_a_feature_branch_is_told_the_default_branch_is_set_up(repo: Path, policy: OrgPolicy):
    """DF-198: the config is on main, not on this old branch: say so, never `init --repo`."""
    git(repo, "branch", "old-feature")
    commit_file(repo, ".agentic/config.yaml", CONFIG)
    git(repo, "switch", "-q", "old-feature")
    error = refused(6, repo, mode="developer", policy=policy)
    assert "main" in f"{error} {error.hint}"
    assert "init --repo" not in str(error)


def test_7_an_unknown_default_branch_says_how_to_find_it(tmp_path: Path):
    """DF-197: no origin/HEAD and no main or master: the hint must not be `git switch main`."""
    repo = tmp_path / "trunk"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "trunk")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", "https://github.com/acme/trunk.git")
    error = refused(7, repo)
    assert "git switch main" not in (error.hint or "")
    assert "set-head" in (error.hint or "")


def test_8_an_untracked_nested_config_refuses_developer_init(repo: Path, policy: OrgPolicy):
    """DF-205: the --others branch of the nested-config search (never committed)."""
    commit_file(repo, ".agentic/config.yaml", CONFIG)
    nested = repo / "pkg" / ".agentic"
    nested.mkdir(parents=True)
    (nested / "config.yaml").write_text(CONFIG, encoding="utf-8")
    error = refused(8, repo, mode="developer", policy=policy)
    assert "pkg/.agentic" in str(error)


def test_8_a_non_ascii_folder_is_named_as_it_is(repo: Path, policy: OrgPolicy):
    """DF-195: git quotes non-ASCII paths unless core.quotePath is off."""
    commit_file(repo, ".agentic/config.yaml", CONFIG)
    commit_file(repo, "café/.agentic/config.yaml", CONFIG)
    error = refused(8, repo, mode="developer", policy=policy)
    assert "café/.agentic" in str(error)
