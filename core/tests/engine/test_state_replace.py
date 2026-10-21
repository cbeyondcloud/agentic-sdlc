"""`run.json` is replaced atomically even while another process reads it (M1-47).

On Windows a rename onto a file that another process holds open fails with
`PermissionError` for a moment (`agentic logs --follow` and `agentic status` read
`run.json` while the engine writes it). The store retries briefly instead of failing
the step."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic.engine import state as state_module
from agentic.engine.state import Cursor, RunState, RunStore


def a_state(run_id: str = "r-1") -> RunState:
    now = datetime.now(UTC)
    return RunState(
        run_id=run_id,
        pipeline="deliver",
        pipeline_version="1.0.0",
        pipeline_digest="sha256:" + "0" * 64,
        policy_version="sha256:" + "1" * 64,
        catalog_digest="sha256:" + "2" * 64,
        regime="team",
        starter="dev@example.invalid",
        status="running",
        cursor=Cursor(index=0),
        started_at=now,
        updated_at=now,
    )


def test_a_replace_blocked_by_a_reader_is_retried(tmp_path: Path, monkeypatch):
    real, calls = os.replace, []

    def busy_twice(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError(13, "the file is being used by another process")
        real(src, dst)

    monkeypatch.setattr(state_module.os, "replace", busy_twice)
    monkeypatch.setattr(state_module, "_REPLACE_WAIT_S", 0)
    store = RunStore(tmp_path)
    store.save(a_state())
    assert len(calls) == 3
    assert store.load("r-1").status == "running"


def test_a_replace_that_keeps_failing_raises_and_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch
):
    def always_busy(src, dst):
        raise PermissionError(13, "busy")

    monkeypatch.setattr(state_module.os, "replace", always_busy)
    monkeypatch.setattr(state_module, "_REPLACE_WAIT_S", 0)
    with pytest.raises(PermissionError):
        RunStore(tmp_path).save(a_state())
    assert list((tmp_path / "r-1").iterdir()) == []
