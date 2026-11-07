"""The learning loop L4-L6 on the CLI (M3-14..M3-16), standalone: `learn list | show | approve
| edit | reject | defer | endorse | propose --from | revert`. A personal learning is written
into the (test) ~/.agentic and reverted through Zyroflo's undo; a shared one is a pull
request on a new branch (a local bare remote and the fake GitHub, never a live host); the
gateway is a fake. Nothing is applied without approve (D29)."""

import json
import subprocess
from pathlib import Path

import httpx2
import pytest
from _cli_support import FakeGateway, answer, git, write
from fake_github import FakeGitHub

from agentic.yamlio import load_yaml
from agentic_cli import runtime
from agentic_cli.exit_codes import ExitCode

KEY = "tl-" + "fake-gateway-key-0314"  # gitleaks:allow (not a real key)
TOKEN = "ghp_" + "LearnApply" + "Fake0123456789"  # gitleaks:allow - built from pieces
HOST = "github.example.test"
CRITERIA = [
    "Acceptance criteria must name every CSV column.",
    "Name every CSV column in the acceptance criteria.",
    "The acceptance criteria should name each CSV column.",
    "Each CSV column must be named by the acceptance criteria.",
]
AGENTS_MD = "# App\n\nUse uv for everything.\n"
PEOPLE = ["alice@acme.example", "bob@acme.example", "carol@acme.example"]


@pytest.fixture
def gateway(workspace, monkeypatch) -> FakeGateway:
    attempts = [answer("requirements", summary=f"Attempt {n}.") for n in range(1, 9)]
    fake = FakeGateway({"ba": attempts, "architect": [answer("design")]})
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    return fake


def started(cli) -> str:
    result = cli("--json", "run", "deliver", "--input", "request=CSV export")
    assert result.code == ExitCode.PAUSED, result.err
    return result.json()["run_id"]


def corrections(cli, monkeypatch, people: list[str], texts=CRITERIA[:3]) -> str:
    """One rejection per text, each by the next person; returns the run id."""
    monkeypatch.setenv("AGENTIC_USER", people[0])
    run_id = started(cli)
    for n, text in enumerate(texts):
        monkeypatch.setenv("AGENTIC_USER", people[n % len(people)])
        result = cli("--json", "reject", run_id, "--feedback", text)
        assert result.code == ExitCode.PAUSED, result.err
    return run_id


def only_proposal(cli) -> dict:
    listed = cli("--json", "learn", "list")
    assert listed.code == ExitCode.OK, listed.err
    rows = listed.json()
    assert len(rows) == 1, rows
    return rows[0]


def error(result) -> dict:
    return json.loads(result.err)


def test_show_has_diff_evidence_and_eval_case(workspace, gateway, cli, monkeypatch):
    corrections(cli, monkeypatch, PEOPLE)
    row = only_proposal(cli)
    assert (row["agent"], row["status"], row["target"]) == ("ba", "pending", "repo")
    shown = cli("--json", "learn", "show", row["id"])
    assert shown.code == ExitCode.OK, shown.err
    draft = shown.json()["draft"]
    assert (draft["scope"], draft["kind"], draft["path"]) == ("repo", "repo-memory", "AGENTS.md")
    assert draft["diff"].startswith("--- /dev/null\n+++ b/AGENTS.md\n")
    assert len(draft["evidence"]) == 3 and {e["actor"] for e in draft["evidence"]} == set(PEOPLE)
    assert draft["eval_case"]["agent"] == "ba" and draft["eval_case"]["expected"]["key_points"]
    assert draft["eval_path"] == f".agentic/eval/cases/scoping/learned-{row['id']}.yaml"
    assert draft["problems"] == []


def test_nothing_is_applied_without_approve(workspace, gateway, cli, monkeypatch, tmp_path):
    corrections(cli, monkeypatch, PEOPLE[:1])
    row = only_proposal(cli)
    assert row["target"] == "personal"
    home = tmp_path / "home"
    for args in (["show", row["id"]], ["endorse", row["id"]], ["defer", row["id"]]):
        assert cli("--json", "learn", *args).code == ExitCode.OK
    refused = cli("--json", "learn", "revert", row["id"])
    assert refused.code == ExitCode.REFUSED
    assert cli("--json", "learn", "reject", row["id"]).code == ExitCode.OK
    assert not (home / "skills").exists() and not (home / "memory").exists()
    assert not (workspace / "AGENTS.md").exists()
    late = cli("--json", "learn", "approve", row["id"])
    assert late.code == ExitCode.REFUSED and "rejected" in error(late)["error"]
    assert not (home / "skills").exists()


def test_personal_approve_applies_and_revert_restores(
    workspace, gateway, cli, monkeypatch, tmp_path
):
    corrections(cli, monkeypatch, PEOPLE[:1])
    row = only_proposal(cli)
    monkeypatch.setenv("AGENTIC_USER", PEOPLE[1])
    other = cli("--json", "learn", "approve", row["id"])
    assert other.code == ExitCode.REFUSED and PEOPLE[0] in error(other)["error"]
    monkeypatch.setenv("AGENTIC_USER", PEOPLE[0])
    done = cli("--json", "learn", "approve", row["id"])
    assert done.code == ExitCode.OK, done.err
    skill = tmp_path / "home" / "skills" / "learned-ba" / "SKILL.md"
    case = tmp_path / "home" / "eval" / "cases" / "scoping" / f"learned-{row['id']}.yaml"
    assert "CSV column" in skill.read_text("utf-8") and case.is_file()
    assert done.json()["status"] == "applied" and done.json()["applied"]["kind"] == "personal"
    reverted = cli("--json", "learn", "revert", row["id"])
    assert reverted.code == ExitCode.OK, reverted.err
    assert not skill.exists() and not case.exists()
    assert only_proposal(cli)["status"] == "reverted"


def test_personal_revert_keeps_a_hand_edit(workspace, gateway, cli, monkeypatch, tmp_path):
    """Review F2: a revert takes out only the learning's block; a line the person added to
    the file since stays, and they are told the file had changed."""
    corrections(cli, monkeypatch, PEOPLE[:1])
    row = only_proposal(cli)
    assert cli("--json", "learn", "approve", row["id"]).code == ExitCode.OK
    skill = tmp_path / "home" / "skills" / "learned-ba" / "SKILL.md"
    skill.write_bytes(skill.read_bytes() + b"\nMy own line.\n")
    reverted = cli("learn", "revert", row["id"])
    assert reverted.code == ExitCode.OK, reverted.err
    text = skill.read_text("utf-8")
    assert "My own line." in text and "CSV column" not in text
    assert "changed since" in " ".join(reverted.err.split())


def test_reject_is_remembered_and_defer_keeps_it_pending(workspace, gateway, cli, monkeypatch):
    run_id = corrections(cli, monkeypatch, PEOPLE[:1])
    row = only_proposal(cli)
    assert cli("--json", "learn", "defer", row["id"]).code == ExitCode.OK
    assert only_proposal(cli)["status"] == "deferred"
    assert cli("--json", "learn", "reject", row["id"], "--reason", "not ours").code == 0
    assert cli("--json", "reject", run_id, "--feedback", CRITERIA[3]).code == ExitCode.PAUSED
    assert only_proposal(cli)["status"] == "rejected"  # one more is not new evidence enough


def test_edit_changes_kind_and_lesson(workspace, gateway, cli, monkeypatch):
    corrections(cli, monkeypatch, PEOPLE[:1])
    row = only_proposal(cli)
    lesson = "List every CSV column by name in the acceptance criteria."
    edited = cli(
        "--json", "learn", "edit", row["id"], "--kind", "personal-note", "--lesson", lesson
    )
    assert edited.code == ExitCode.OK, edited.err
    draft = cli("--json", "learn", "show", row["id"]).json()["draft"]
    assert (draft["kind"], draft["path"], draft["lesson"]) == (
        "personal-note",
        "memory/learned-ba.md",
        lesson,
    )
    bad = cli("--json", "learn", "edit", row["id"], "--kind", "bogus")
    assert bad.code == ExitCode.INVALID


def test_propose_from_one_feedback_record(workspace, gateway, cli, monkeypatch):
    corrections(cli, monkeypatch, PEOPLE[:1], texts=CRITERIA[:1])
    assert cli("--json", "learn", "list").json() == []
    fid = cli("--json", "learn", "feedback").json()[0]["id"]
    made = cli("--json", "learn", "propose", "--from", fid)
    assert made.code == ExitCode.OK, made.err
    row = only_proposal(cli)
    assert (row["status"], row["evidence"]) == ("pending", 1)
    again = cli("--json", "learn", "propose", "--from", fid)
    assert again.code == ExitCode.OK and again.json()["id"] == row["id"]


def test_end_of_run_shows_pending_proposals(workspace, gateway, cli, monkeypatch):
    monkeypatch.setenv("AGENTIC_USER", PEOPLE[0])
    run_id = started(cli)
    for text in CRITERIA[:2]:
        result = cli("reject", run_id, "--feedback", text)
        assert "learning proposal" not in result.err
    third = cli("reject", run_id, "--feedback", CRITERIA[2])  # warnings: not in --json
    assert third.code == ExitCode.PAUSED
    assert "1 learning proposal(s) wait for a decision" in " ".join(third.err.split())
    assert "agentic learn list" in third.err


def test_bad_agent_name_and_run_id_are_usage_errors(workspace, gateway, cli, monkeypatch):
    """DF-730: a bad agent name or run id is a usage error with a hint, not a traceback."""
    corrections(cli, monkeypatch, PEOPLE[:1], texts=CRITERIA[:1])
    fid = cli("--json", "learn", "feedback").json()[0]["id"]
    bad = cli("--json", "learn", "attribute", fid, "--agent", "Bad Name", "--reason", "x")
    assert bad.code == ExitCode.USAGE and error(bad)["hint"]
    run = cli("--json", "learn", "feedback", "--run", "../outside")
    assert run.code == ExitCode.USAGE and error(run)["hint"]


def test_corrupt_proposals_file_has_a_hint(workspace, gateway, cli, monkeypatch):
    """DF-736: a damaged proposals file is named, with what to do about it."""
    corrections(cli, monkeypatch, PEOPLE[:1], texts=CRITERIA[:1])
    write(workspace / ".agentic" / "local" / "learning" / "proposals.json", "{not json")
    listed = cli("--json", "learn", "list")
    assert listed.code == ExitCode.INVALID
    assert "proposals.json" in error(listed)["error"] and "rebuil" in error(listed)["hint"]


# ── end to end: three corrections -> proposal -> approve -> PR -> revert (M3-16) ──


@pytest.fixture
def github(tmp_path, workspace, monkeypatch):
    write(workspace / "AGENTS.md", AGENTS_MD)
    git(workspace, "add", "AGENTS.md")
    git(workspace, "commit", "-qm", "agents")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git(workspace, "remote", "add", "origin", f"https://{HOST}/acme/app.git")
    git(workspace, "remote", "set-url", "--push", "origin", str(remote))
    git(workspace, "push", "-q", "origin", "main")
    fake = FakeGitHub(TOKEN, remote).start()
    write(
        tmp_path / "home" / "config.yaml",
        f"git_hosts:\n  {HOST}:\n    kind: github\n    api_url: {fake.url}\n",
    )
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", TOKEN)
    yield fake, remote
    fake.stop()


def at(remote: Path, ref: str, path: str) -> str:
    """A file's exact bytes at `ref` in the bare remote (the `git` helper strips output)."""
    shown = ["git", "-C", str(remote), "show", f"{ref}:{path}"]
    return subprocess.run(shown, capture_output=True, check=True).stdout.decode("utf-8")


def test_e2e_three_corrections_to_pr_and_revert(workspace, gateway, github, cli, monkeypatch):
    fake, remote = github
    main_before = git(remote, "rev-parse", "main").strip()
    corrections(cli, monkeypatch, PEOPLE)
    row = only_proposal(cli)
    assert row["target"] == "repo"

    done = cli("learn", "approve", row["id"])  # warnings show outside --json
    assert done.code == ExitCode.OK, done.err
    assert "no RBAC" in done.err  # standalone: the maintainers decide in the PR review
    applied = cli("--json", "learn", "show", row["id"]).json()["applied"]
    assert applied["kind"] == "pull-request"
    assert applied["branch"] == f"agentic/learning/{row['id']}"
    assert git(remote, "rev-parse", "main").strip() == main_before  # never the default branch
    learned = at(remote, applied["branch"], "AGENTS.md")
    assert learned.startswith(AGENTS_MD) and "CSV column" in learned
    case = at(remote, applied["branch"], f".agentic/eval/cases/scoping/learned-{row['id']}.yaml")
    assert "key_points" in case
    assert [p["head"]["ref"] for p in fake.pulls] == [applied["branch"]]
    assert "CSV column" in fake.pulls[0]["body"] and row["id"] in fake.pulls[0]["body"]

    # a maintainer merges it on the host
    git(remote, "update-ref", "refs/heads/main", applied["commit"])
    fake.merge(1)
    # the test host has no fetch URL: the developer's last fetch is the base (said so)
    git(workspace, "fetch", "-q", str(remote), "main:refs/remotes/origin/main")

    reverted = cli("--json", "learn", "revert", row["id"])
    assert reverted.code == ExitCode.OK, reverted.err
    back = reverted.json()["reverted"]
    assert back["kind"] == "pull-request"
    assert back["branch"] == f"agentic/learning/{row['id']}-revert"
    assert at(remote, back["branch"], "AGENTS.md") == AGENTS_MD  # the previous content
    assert len(fake.pulls) == 2 and fake.pulls[1]["head"]["ref"] == back["branch"]
    assert only_proposal(cli)["status"] == "reverted"


# ── a change that cannot be made leaves no dead end (review F3) ──


def test_no_remote_leaves_the_proposal_pending(workspace, gateway, cli, monkeypatch):
    corrections(cli, monkeypatch, PEOPLE)
    row = only_proposal(cli)
    sides = measuring(gateway, monkeypatch, "x")
    refused = cli("--json", "learn", "approve", row["id"])
    assert refused.code == ExitCode.REFUSED and "remote" in error(refused)["hint"]
    assert only_proposal(cli)["status"] == "pending"  # nothing decided: nothing was made
    assert sides == []  # review I1: nothing measured (no model spend) before a refusal
    assert cli("--json", "learn", "reject", row["id"]).code == ExitCode.OK


def test_a_failed_apply_is_made_by_approving_again(
    workspace, gateway, github, cli, monkeypatch, tmp_path
):
    fake, remote = github
    corrections(cli, monkeypatch, PEOPLE)
    row = only_proposal(cli)
    git(workspace, "remote", "set-url", "--push", "origin", str(tmp_path / "missing.git"))
    failed = cli("--json", "learn", "approve", row["id"])
    assert failed.code == ExitCode.ERROR, failed.err
    assert only_proposal(cli)["status"] == "approved"
    failed_measurement = cli("--json", "learn", "show", row["id"]).json()["measurement"]
    assert failed_measurement["status"] == "measured"
    stuck = cli("--json", "learn", "revert", row["id"])
    assert stuck.code == ExitCode.REFUSED and "learn approve" in error(stuck)["hint"]

    git(workspace, "remote", "set-url", "--push", "origin", str(remote))
    sides = measuring(gateway, monkeypatch, "x")
    again = cli("--json", "learn", "approve", row["id"])
    assert again.code == ExitCode.OK, again.err
    assert again.json()["status"] == "applied" and len(fake.pulls) == 1
    assert sides == []  # the first attempt's measurement is kept: no second spend
    assert again.json()["measurement"] == failed_measurement


def test_an_approved_change_that_is_not_made_can_be_rejected(
    workspace, gateway, github, cli, monkeypatch, tmp_path
):
    corrections(cli, monkeypatch, PEOPLE)
    row = only_proposal(cli)
    git(workspace, "remote", "set-url", "--push", "origin", str(tmp_path / "missing.git"))
    assert cli("--json", "learn", "approve", row["id"]).code == ExitCode.ERROR
    assert cli("--json", "learn", "reject", row["id"]).code == ExitCode.OK
    assert only_proposal(cli)["status"] == "rejected"


def test_a_target_missing_on_the_default_branch_is_refused_with_a_hint(
    workspace, gateway, github, cli, monkeypatch
):
    """The agent definition exists only in this checkout: the change cannot be made on the
    default branch, which is said before anything is decided."""
    fake, _ = github
    corrections(cli, monkeypatch, PEOPLE)
    row = only_proposal(cli)
    write(workspace / ".agentic" / "agents" / "ba.md", "---\nname: ba\n---\n\nYou analyse.\n")
    edited = cli("--json", "learn", "edit", row["id"], "--kind", "agent-instruction")
    assert edited.code == ExitCode.OK, edited.err
    sides = measuring(gateway, monkeypatch, "x")
    refused = cli("--json", "learn", "approve", row["id"])
    assert refused.code == ExitCode.INVALID, refused.err
    assert "learn edit" in error(refused)["hint"]
    assert only_proposal(cli)["status"] == "pending" and fake.pulls == []
    assert sides == []  # review I1: refused on the default branch before any model call


# ── the regression case before and after, on approve (M3-16, DF-756) ──


def measuring(gateway: FakeGateway, monkeypatch, learned: str) -> list[str]:
    """The gateway as the eval sees it: an agent whose instructions hold the learned block
    answers with the lesson's words, one without it does not (no model is called). Returns
    the learning-eval requests' sides, in order."""
    sides: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("x-agentic-step") != "learning-eval":
            return gateway.handler(request)
        side = "after" if "Learned from corrections" in request.content.decode() else "before"
        sides.append(side)
        summary = learned if side == "after" else "Export the invoices."
        gateway.answers["ba"].insert(0, answer("requirements", summary=summary))
        return gateway.handler(request)

    monkeypatch.setattr(runtime, "network_transport", lambda: httpx2.MockTransport(handler))
    return sides


def test_approve_measures_the_case_before_and_after(workspace, gateway, cli, monkeypatch, tmp_path):
    corrections(cli, monkeypatch, PEOPLE[:1])
    row = only_proposal(cli)
    sides = measuring(gateway, monkeypatch, "Acceptance criteria name every CSV column.")
    done = cli("--json", "learn", "approve", row["id"])
    assert done.code == ExitCode.OK, done.err
    measured = done.json()["measurement"]
    assert sorted(sides) == ["after", "before"]
    assert measured["status"] == "measured" and measured["case"] == f"learned-{row['id']}"
    assert measured["before"] < measured["after"] and not measured["regressed"]
    assert cli("--json", "learn", "show", row["id"]).json()["measurement"] == measured
    ledger = workspace / ".agentic" / "local" / "runs" / measured["ledger"]
    assert ledger.is_dir()  # the eval's run ledger (Zyroflo's hash chain)
    assert cli("--json", "audit", "verify", measured["ledger"]).code == ExitCode.OK


def test_a_regression_is_said_and_the_approval_stands(workspace, gateway, cli, monkeypatch):
    corrections(cli, monkeypatch, PEOPLE[:1])
    row = only_proposal(cli)
    sides: list[str] = []

    def worse(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("x-agentic-step") == "learning-eval":
            sides.append("x")
            text = request.content.decode()
            summary = "Export." if "Learned from corrections" in text else CRITERIA[0]
            gateway.answers["ba"].insert(0, answer("requirements", summary=summary))
        return gateway.handler(request)

    monkeypatch.setattr(runtime, "network_transport", lambda: httpx2.MockTransport(worse))
    done = cli("learn", "approve", row["id"])
    assert done.code == ExitCode.OK, done.err
    assert "scored lower with the change" in " ".join(done.err.split())
    shown = cli("--json", "learn", "show", row["id"]).json()
    assert shown["status"] == "applied" and shown["measurement"]["regressed"]


def test_approve_no_eval_skips_the_measurement(workspace, gateway, cli, monkeypatch):
    corrections(cli, monkeypatch, PEOPLE[:1])
    row = only_proposal(cli)
    sides = measuring(gateway, monkeypatch, "x")
    done = cli("--json", "learn", "approve", row["id"], "--no-eval")
    assert done.code == ExitCode.OK, done.err
    assert sides == [] and done.json()["measurement"]["status"] == "skipped"


def test_an_unmeasurable_case_never_blocks_the_approval(workspace, gateway, cli, monkeypatch):
    corrections(cli, monkeypatch, PEOPLE[:1])
    row = only_proposal(cli)
    monkeypatch.delenv("AGENTIC_GATEWAY_URL")
    done = cli("learn", "approve", row["id"])
    assert done.code == ExitCode.OK, done.err
    assert "not run before and after" in " ".join(done.err.split())
    shown = cli("--json", "learn", "show", row["id"]).json()
    assert shown["status"] == "applied" and shown["measurement"]["status"] == "unmeasured"
    assert "gateway" in shown["measurement"]["reason"]


# ── a team learning is a policy-repo pull request for that team's leads (D30, DF-757) ──

POLICY_HOST = "policy.example.test"
TEAM_CATALOG = "# The payments team's own skills.\nskills: {}\n"
SKILL = "teams/payments/skills/internal/learned-ba/SKILL.md"
CATALOG = "teams/payments/skills/catalog.yaml"


@pytest.fixture
def policy_host(tmp_path, workspace, monkeypatch):
    """The policy repo on a (fake) host: a bare remote, the fake GitHub and the approver's
    checkout of it (`policy_checkout`), its catalog committed on main."""
    bare = tmp_path / "policy.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    checkout = tmp_path / "policy-checkout"
    subprocess.run(["git", "init", "-q", "-b", "main", str(checkout)], check=True)
    git(checkout, "config", "user.email", "lead@acme.example")
    git(checkout, "config", "user.name", "Lead")
    write(checkout / CATALOG, TEAM_CATALOG)
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "policy")
    git(checkout, "remote", "add", "origin", f"https://{POLICY_HOST}/acme/policy.git")
    git(checkout, "remote", "set-url", "--push", "origin", str(bare))
    git(checkout, "push", "-q", "origin", "main")
    git(checkout, "update-ref", "refs/remotes/origin/main", "main")
    fake = FakeGitHub(TOKEN, bare).start()
    write(
        tmp_path / "home" / "config.yaml",
        f"policy_checkout: {checkout.as_posix()}\n"
        f"git_hosts:\n  {POLICY_HOST}:\n    kind: github\n    api_url: {fake.url}\n",
    )
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", TOKEN)
    yield fake, bare, checkout
    fake.stop()


def yaml_load(text: str) -> dict:
    return load_yaml(text)[0]


def folder_sha(bare: Path, ref: str, tmp_path: Path) -> str:
    from agentic.skills.authoring import skill_sha

    folder = tmp_path / "sha" / ref.replace("/", "-") / "learned-ba"
    write(folder / "SKILL.md", at(bare, ref, SKILL))
    return skill_sha(folder)


def test_team_learning_opens_a_policy_repo_pull_request_and_reverts(
    workspace, gateway, policy_host, cli, monkeypatch, tmp_path
):
    fake, bare, checkout = policy_host
    main_before = git(bare, "rev-parse", "main").strip()
    corrections(cli, monkeypatch, PEOPLE)
    row = only_proposal(cli)
    edited = cli("--json", "learn", "edit", row["id"], "--kind", "team-skill")
    assert edited.code == ExitCode.OK, edited.err
    assert (
        edited.json()["eval_path"] == f"teams/payments/eval/cases/scoping/learned-{row['id']}.yaml"
    )

    done = cli("learn", "approve", row["id"], "--no-eval")
    assert done.code == ExitCode.OK, done.err
    assert "team's leads" in " ".join(done.err.split())  # standalone: they decide in review
    applied = cli("--json", "learn", "show", row["id"]).json()["applied"]
    assert applied["kind"] == "pull-request" and applied["repo"].endswith("acme/policy")
    branch = applied["branch"]
    assert branch == f"agentic/learning/{row['id']}"
    assert git(bare, "rev-parse", "main").strip() == main_before  # never the default branch
    assert "CSV column" in at(bare, branch, SKILL)
    assert "key_points" in at(bare, branch, edited.json()["eval_path"])
    catalog = at(bare, branch, CATALOG)
    assert catalog.startswith("# The payments team's own skills.")
    pinned = yaml_load(catalog)["skills"]["internal/learned-ba"]
    assert pinned["sha256"] == folder_sha(bare, branch, tmp_path)
    assert pinned["path"] == "teams/payments/skills/internal/learned-ba"
    changed = git(bare, "diff", "--name-only", main_before, branch).split()
    assert changed and all(p.startswith("teams/payments/") for p in changed)
    assert "payments" in fake.pulls[0]["body"] and "team's leads" in fake.pulls[0]["body"]

    # a team lead merges it on the host; the approver's checkout fetched it
    git(bare, "update-ref", "refs/heads/main", applied["commit"])
    fake.merge(1)
    git(checkout, "fetch", "-q", str(bare), "main:refs/remotes/origin/main")

    reverted = cli("--json", "learn", "revert", row["id"])
    assert reverted.code == ExitCode.OK, reverted.err
    back = reverted.json()["reverted"]
    assert back["kind"] == "pull-request" and back["branch"] == f"{branch}-revert"
    assert "CSV column" not in at(bare, back["branch"], SKILL)
    repinned = yaml_load(at(bare, back["branch"], CATALOG))["skills"]["internal/learned-ba"]
    assert repinned["sha256"] == folder_sha(bare, back["branch"], tmp_path)
    assert repinned["sha256"] != pinned["sha256"]


def test_reverting_an_unmerged_learning_closes_its_pull_request(
    workspace, gateway, github, cli, monkeypatch
):
    """DF-764: a learning reverted before its pull request is merged closes that pull
    request, so a later merge cannot land a learning that reads as reverted."""
    fake, remote = github
    corrections(cli, monkeypatch, PEOPLE)
    row = only_proposal(cli)
    assert cli("--json", "learn", "approve", row["id"], "--no-eval").code == ExitCode.OK
    reverted = cli("--json", "learn", "revert", row["id"])
    assert reverted.code == ExitCode.OK, reverted.err
    assert reverted.json()["reverted"]["kind"] == "closed"
    assert fake.pulls[0]["state"] == "closed" and not fake.pulls[0].get("merged")
    assert len(fake.pulls) == 1  # no revert pull request: nothing was merged
    assert only_proposal(cli)["status"] == "reverted"


def test_a_team_learning_without_a_policy_repo_is_refused_first(
    workspace, gateway, cli, monkeypatch
):
    """Nothing is measured (no model spend) or decided when the policy repo to open the
    pull request in is not configured."""
    corrections(cli, monkeypatch, PEOPLE)
    row = only_proposal(cli)
    assert cli("--json", "learn", "edit", row["id"], "--kind", "team-skill").code == 0
    sides = measuring(gateway, monkeypatch, "x")
    refused = cli("--json", "learn", "approve", row["id"])
    assert refused.code == ExitCode.REFUSED, refused.err
    assert "policy_checkout" in error(refused)["hint"]
    assert sides == [] and only_proposal(cli)["status"] == "pending"


def test_a_learned_files_skeleton_is_recognised_for_its_own_file_only(tmp_path):
    """DF-768: the revert's `leftover` test is the learned file's skeleton (front matter and
    title, no block, either line ending), for that learning's own file only."""
    from datetime import UTC, datetime

    from agentic.learning.changes import Evidence, draft, learned_skeleton
    from agentic.learning.clustering import Proposal
    from agentic_cli.learning_apply import skeleton_left

    at = datetime(2026, 11, 1, tzinfo=UTC)
    pid = "lp-" + "9" * 20
    proposal = Proposal(
        id=pid,
        agent="ba",
        scope="repo:local",
        lesson="Name every column.",
        evidence=("fb-1",),
        sources=("s",),
        actors=("alice",),
        created_at=at,
    )
    seen = Evidence(
        feedback_id="fb-1", signal="gate-rejection", actor="alice", given_at=at, run_id="run-1"
    )
    made = draft(proposal, [seen], lambda root, path: None, kind="personal-note", at=at)
    left = skeleton_left(made)
    skeleton = learned_skeleton("personal-note", "ba")
    assert left(skeleton, made.path) and left(skeleton.replace("\n", "\r\n"), made.path)
    assert not left(skeleton + "My own line.\n", made.path)
    assert not left(skeleton, "memory/other.md")


def test_an_unmeasured_reason_is_redacted_and_short(workspace, gateway, monkeypatch):
    """Review I2: why a case could not run is kept with the change and sent to the control
    plane, so a secret in it is masked and it is cut short."""
    from agentic.learning.changes import Draft
    from agentic_cli import learning_eval
    from agentic_cli.runtime import Runtime
    from agentic_cli.workspace import Workspace

    def broken(rt, made):
        raise RuntimeError(f"the gateway said no to {KEY} " + "x" * 1000)

    monkeypatch.setattr(learning_eval, "_measure", broken)
    made = Draft.model_construct(
        proposal_id="lp-1", agent="ba", lesson="Name it.", eval_case={"id": "learned-1"}
    )
    warned: list[str] = []
    found = learning_eval.measure_change(
        Runtime(Workspace.load()), made, skip=None, warn=warned.append
    )
    assert found["status"] == "unmeasured" and KEY not in found["reason"]
    assert len(found["reason"]) <= learning_eval.MAX_REASON and KEY not in warned[0]


def test_the_measurement_never_takes_a_cached_answer(workspace, gateway, cli, monkeypatch):
    """DF-1004: before and after each get a new answer from the gateway, never its cache."""
    corrections(cli, monkeypatch, PEOPLE[:1])
    row = only_proposal(cli)
    measuring(gateway, monkeypatch, "Acceptance criteria name every CSV column.")
    inner, fresh = runtime.network_transport(), []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("x-agentic-step") == "learning-eval":
            fresh.append(request.headers.get("x-no-cache"))
        return inner.handler(request)

    monkeypatch.setattr(runtime, "network_transport", lambda: httpx2.MockTransport(handler))
    assert cli("--json", "learn", "approve", row["id"]).code == ExitCode.OK
    assert fresh and set(fresh) == {"true"}
