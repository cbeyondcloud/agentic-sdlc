"""`agentic gc` and the disk cap of `agentic status` (M3-08).

gc prunes finished runs (completed, failed, stopped, killed) older than a cut-off - their
run folder and their trees - and never a run that is running, paused or waiting at a gate,
never one whose events the control plane has not taken (org mode), never one whose tree
holds uncommitted work, and never one whose folder another program holds open (Windows)."""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cli_helpers import git

import sdlc_commons.ledger as ledger_module
from agentic.engine.state import Cursor, RunState, RunStore
from agentic.yamlio import dump_yaml
from agentic_cli import housekeeping
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main
from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.worktrees import RunWorktrees

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
POLICY = "git:" + "c" * 40
ALL_STATUSES = ("running", "awaiting_gate", "paused", "completed", "failed", "stopped", "killed")


@pytest.fixture(autouse=True)
def setup(home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})
    monkeypatch.chdir(repo)
    monkeypatch.setattr(housekeeping, "utc_now", lambda: NOW)
    # the runs folder is git-ignored, as `agentic init` makes it
    (repo / ".gitignore").write_text(".agentic/local/\n.sdlc-worktrees/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore local data")


def write_config(home: Path, **settings) -> None:
    folder = home / ".agentic"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(dump_yaml(settings), encoding="utf-8")


def make_run(
    repo: Path,
    run_id: str,
    status: str,
    days_old: float,
    *,
    events: int = 0,
    delivered: int | None = None,
    payload: int = 1000,
) -> Path:
    if events:
        ledger = RunLedger(repo, run_id)
        ledger.open(
            identity=RunIdentity(team="payments", policy_version=POLICY),
            kind="feature",
            project="app",
            owner="payments",
            criticality="low",
        )
        for n in range(events - 1):  # open() records the first
            ledger.record("step_started", phase="requirements", attempt=n + 1)
    when = NOW - timedelta(days=days_old)
    state = RunState(
        run_id=run_id,
        pipeline="feature-delivery",
        pipeline_version="1.0.0",
        pipeline_digest="sha256:" + "0" * 64,
        policy_version=POLICY,
        catalog_digest="sha256:" + "1" * 64,
        regime="solo",
        starter="dev@example.invalid",
        status=status,
        cursor=Cursor(index=0),
        started_at=when,
        updated_at=when,
    )
    store = RunStore(repo / ".agentic" / "local" / "runs")
    store.save(state)
    folder = store.path(run_id).parent
    (folder / "logs").mkdir(exist_ok=True)
    (folder / "logs" / "dev-1.jsonl").write_bytes(b"x" * payload)
    if delivered is not None:
        (folder / "sinks").mkdir(exist_ok=True)
        cursor = {"seq": delivered, "hash": "h"}
        (folder / "sinks" / "control-plane.json").write_text(json.dumps(cursor), encoding="utf-8")
    return folder


def gc(capsys, *args: str) -> tuple[int, dict]:
    code = main(["--json", "gc", *args])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def runs_dir(repo: Path) -> Path:
    return repo / ".agentic" / "local" / "runs"


# ── which runs go ─────────────────────────────────────────────────────────────


def test_only_finished_old_runs_are_pruned(repo: Path, capsys):
    for status in ALL_STATUSES:
        make_run(repo, f"old-{status.replace('_', '-')}", status, days_old=30)
        make_run(repo, f"new-{status.replace('_', '-')}", status, days_old=1)

    code, data = gc(capsys)

    assert code == ExitCode.OK
    removed = sorted(r["run_id"] for r in data["removed"])
    assert removed == ["old-completed", "old-failed", "old-killed", "old-stopped"]
    left = sorted(p.name for p in runs_dir(repo).iterdir())
    assert left == sorted(
        [f"new-{s.replace('_', '-')}" for s in ALL_STATUSES]
        + ["old-awaiting-gate", "old-paused", "old-running"]
    )
    kept = {k["run_id"]: k["reason"] for k in data["kept"]}
    assert set(kept) == {"old-awaiting-gate", "old-paused", "old-running"}
    assert "not finished" in kept["old-paused"]


def test_dry_run_lists_sizes_and_changes_nothing(repo: Path, capsys):
    make_run(repo, "r-old", "completed", days_old=30, payload=5000)
    before = sorted(str(p) for p in runs_dir(repo).rglob("*"))

    code, data = gc(capsys, "--dry-run")

    assert code == ExitCode.OK
    assert data["dry_run"] is True
    (entry,) = data["removed"]
    assert entry["run_id"] == "r-old"
    assert entry["bytes"] >= 5000
    assert data["freed_bytes"] == entry["bytes"]
    assert sorted(str(p) for p in runs_dir(repo).rglob("*")) == before


def test_dry_run_human_output_names_the_runs_and_sizes(repo: Path, capsys):
    make_run(repo, "r-old", "completed", days_old=30, payload=5000)
    assert main(["gc", "--dry-run"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "r-old" in out
    assert "would remove" in out
    assert "KB" in out


def test_older_than_flag_overrides_config_and_policy(repo: Path, home: Path, capsys):
    write_config(home, gc={"older_than_days": 60})
    make_run(repo, "r-40", "completed", days_old=40)
    make_run(repo, "r-10", "completed", days_old=10)

    _, by_config = gc(capsys, "--dry-run")
    assert by_config["older_than_days"] == 60
    assert by_config["older_than_source"] == "user config"
    assert by_config["removed"] == []

    _, by_flag = gc(capsys, "--dry-run", "--older-than", "5")
    assert by_flag["older_than_days"] == 5
    assert sorted(r["run_id"] for r in by_flag["removed"]) == ["r-10", "r-40"]


def test_policy_default_applies_without_flag_or_config(repo: Path, capsys, monkeypatch):
    from agentic.policy.model import LocalDataPolicy

    monkeypatch.setattr(
        housekeeping, "_policy_local", lambda ws: LocalDataPolicy(gc_after_days=3, disk_cap_gb=1)
    )
    make_run(repo, "r-5", "completed", days_old=5)
    _, data = gc(capsys, "--dry-run")
    assert data["older_than_days"] == 3
    assert data["older_than_source"] == "policy"
    assert [r["run_id"] for r in data["removed"]] == ["r-5"]


def test_built_in_default_is_fourteen_days(repo: Path, capsys):
    make_run(repo, "r-20", "completed", days_old=20)
    make_run(repo, "r-10", "completed", days_old=10)
    _, data = gc(capsys, "--dry-run")
    assert data["older_than_days"] == 14
    assert [r["run_id"] for r in data["removed"]] == ["r-20"]


@pytest.mark.parametrize("bad", ["0", "-3", "x"])
def test_bad_older_than_is_a_usage_error(repo: Path, capsys, bad: str):
    assert main(["gc", "--older-than", bad]) == ExitCode.USAGE


def test_bad_gc_config_is_invalid(repo: Path, home: Path, capsys):
    write_config(home, gc={"older_than_days": "soon"})
    assert main(["gc"]) == ExitCode.INVALID


def test_unreadable_run_json_is_kept(repo: Path, capsys):
    folder = make_run(repo, "r-bad", "completed", days_old=30)
    (folder / "run.json").write_text("{broken", encoding="utf-8")
    _, data = gc(capsys)
    assert data["removed"] == []
    assert folder.is_dir()


# ── org mode: queued events survive ───────────────────────────────────────────


def test_org_mode_keeps_undelivered_runs(repo: Path, home: Path, capsys):
    write_config(home, org="acme", control_plane_url="https://cp.example.invalid")
    make_run(repo, "r-sent", "completed", days_old=30, events=3, delivered=3)
    make_run(repo, "r-queued", "completed", days_old=30, events=3, delivered=1)
    make_run(repo, "r-never", "completed", days_old=30, events=2)

    _, data = gc(capsys)

    assert [r["run_id"] for r in data["removed"]] == ["r-sent"]
    kept = {k["run_id"]: k["reason"] for k in data["kept"]}
    assert "2 event(s) not yet delivered" in kept["r-queued"]
    assert "agentic sync" in kept["r-queued"]
    assert "2 event(s)" in kept["r-never"]
    assert (runs_dir(repo) / "r-queued" / "events.jsonl").is_file()


def test_org_mode_without_control_plane_keeps_runs(repo: Path, home: Path, capsys):
    write_config(home, org="acme")
    make_run(repo, "r-1", "completed", days_old=30, events=2)
    _, data = gc(capsys)
    assert data["removed"] == []
    (kept,) = data["kept"]
    assert "no control plane" in kept["reason"]


def test_standalone_runs_need_no_delivery(repo: Path, capsys):
    make_run(repo, "r-1", "completed", days_old=30, events=2)
    _, data = gc(capsys)
    assert [r["run_id"] for r in data["removed"]] == ["r-1"]


# ── trees ─────────────────────────────────────────────────────────────────────


def tree_for(repo: Path, run_id: str) -> Path:
    (tree,) = RunWorktrees(repo, run_id).create().values()
    return tree.path


def test_trees_are_removed_with_the_run(repo: Path, capsys):
    make_run(repo, "r-1", "completed", days_old=30, events=1)
    tree = tree_for(repo, "r-1")
    (tree / "big.bin").write_bytes(b"y" * 4000)
    git(tree, "add", "big.bin")
    git(tree, "commit", "-qm", "work")

    _, plan = gc(capsys, "--dry-run")
    assert plan["removed"][0]["bytes"] >= 5000  # run folder and tree

    code, data = gc(capsys)

    assert code == ExitCode.OK
    assert [r["run_id"] for r in data["removed"]] == ["r-1"]
    assert not tree.exists()
    assert not (runs_dir(repo) / "r-1").exists()
    assert "r-1" not in git(repo, "worktree", "list")
    assert git(repo, "branch", "--list", "sdlc/r-1")  # the branch is kept: a PR may name it


def test_dirty_tree_keeps_the_run(repo: Path, capsys):
    make_run(repo, "r-1", "completed", days_old=30, events=1)
    tree = tree_for(repo, "r-1")
    (tree / "src" / "app.py").write_text("x = 2  # unfinished\n", encoding="utf-8")

    _, plan = gc(capsys, "--dry-run")
    assert plan["removed"] == []
    assert "uncommitted" in plan["kept"][0]["reason"]

    _, data = gc(capsys)
    assert data["removed"] == []
    assert tree.exists()
    assert (runs_dir(repo) / "r-1" / "run.json").is_file()


def test_read_only_files_are_deleted(repo: Path, capsys):
    folder = make_run(repo, "r-1", "completed", days_old=30)
    locked = folder / "logs" / "dev-1.jsonl"
    os.chmod(locked, stat.S_IREAD)
    _, data = gc(capsys)
    assert [r["run_id"] for r in data["removed"]] == ["r-1"]
    assert not folder.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to rename an open folder")
def test_locked_run_folder_is_kept_on_windows(repo: Path, capsys):
    folder = make_run(repo, "r-1", "completed", days_old=30)
    with (folder / "logs" / "dev-1.jsonl").open("rb"):
        _, data = gc(capsys)
    assert data["removed"] == []
    (kept,) = data["kept"]
    assert "in use" in kept["reason"]
    assert (folder / "run.json").is_file()
    assert not [p for p in runs_dir(repo).iterdir() if p.name.startswith(".gc-")]


def test_leftover_trash_is_swept(repo: Path, capsys):
    trash = runs_dir(repo) / ".gc-r-old-123"
    (trash / "logs").mkdir(parents=True)
    (trash / "logs" / "x.jsonl").write_text("x", encoding="utf-8")
    code, _ = gc(capsys)
    assert code == ExitCode.OK
    assert not trash.exists()


def test_no_runs_folder_is_fine(repo: Path, capsys):
    code, data = gc(capsys)
    assert code == ExitCode.OK
    assert data["removed"] == [] and data["kept"] == []


# ── the disk cap in `agentic status` ──────────────────────────────────────────


def test_status_warns_over_the_cap(repo: Path, home: Path, capsys):
    write_config(home, gc={"disk_cap_gb": 0.000001})  # about 1 KB
    make_run(repo, "r-1", "completed", days_old=1, payload=5000)
    assert main(["status"]) == ExitCode.OK
    err = capsys.readouterr().err
    assert "local data" in err
    assert "agentic gc" in err


def test_status_quiet_under_the_cap(repo: Path, capsys):
    make_run(repo, "r-1", "completed", days_old=1, payload=5000)
    assert main(["status"]) == ExitCode.OK
    assert "local data" not in capsys.readouterr().err


def test_local_usage_stops_at_the_cap(repo: Path):
    for n in range(5):
        make_run(repo, f"r-{n}", "completed", days_old=1, payload=10_000)
    usage = housekeeping.local_usage(repo, stop_at=15_000, deadline_s=10)
    assert usage.bytes > 15_000
    assert not usage.complete
    whole = housekeeping.local_usage(repo, stop_at=None, deadline_s=10)
    assert whole.complete and whole.bytes >= 50_000
