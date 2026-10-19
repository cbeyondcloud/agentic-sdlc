"""Tests for the run state file: run.json, written atomically (M1-21, spec §8.3)."""

from datetime import UTC, datetime

import pytest

from agentic.engine.state import Cursor, Pending, RunState, RunStateError, RunStore, StepRecord

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def new_state(run_id: str = "r-1", **changes) -> RunState:
    base = {
        "run_id": run_id,
        "pipeline": "feature-delivery",
        "pipeline_version": "1.0.0",
        "pipeline_digest": "sha256:" + "a" * 64,
        "policy_version": "sha256:" + "b" * 64,
        "catalog_digest": "sha256:" + "c" * 64,
        "regime": "team",
        "starter": "dev@acme.example",
        "status": "running",
        "cursor": Cursor(index=0),
        "inputs": {"request": "Add CSV export"},
        "started_at": T0,
        "updated_at": T0,
    }
    return RunState(**{**base, **changes})


def test_save_then_load_round_trips(tmp_path):
    store = RunStore(tmp_path)
    state = new_state(
        history=[
            StepRecord(
                step="design",
                iteration=1,
                outcome="completed",
                handoff="handoffs/design-1.md",
                digest="sha256:" + "d" * 64,
                cost_usd="0.0123",
                started_at=T0,
                ended_at=T0,
            )
        ],
        pending=Pending(
            kind="gate",
            id="design-approval",
            reason="waiting for approval",
            artefacts={"handoffs/design-1.md": "sha256:" + "d" * 64},
            since=T0,
        ),
        latest={"design": "handoffs/design-1.md"},
        budget={"name": "run", "scope": "run"},
    )

    store.save(state)
    assert store.load("r-1") == state
    assert store.exists("r-1") and not store.exists("r-2")


def test_run_json_is_lf_utf8_in_the_run_folder(tmp_path):
    store = RunStore(tmp_path)
    store.save(new_state(inputs={"request": "Exporter les factures - été"}))

    path = store.path("r-1")
    assert path == tmp_path / "r-1" / "run.json"
    data = path.read_bytes()
    assert b"\r\n" not in data and "été".encode() in data


def test_a_crash_while_saving_leaves_the_previous_state(tmp_path, monkeypatch):  # RF 1
    store = RunStore(tmp_path)
    store.save(new_state())

    def power_cut(src, dst):
        raise OSError("power cut")

    monkeypatch.setattr("agentic.engine.state.os.replace", power_cut)
    with pytest.raises(OSError, match="power cut"):
        store.save(new_state(status="completed"))

    assert store.load("r-1").status == "running"
    assert [p.name for p in store.path("r-1").parent.iterdir() if ".tmp" in p.name] == []


def test_a_damaged_run_json_is_a_clear_error(tmp_path):
    store = RunStore(tmp_path)
    store.save(new_state())
    store.path("r-1").write_text("{not json", encoding="utf-8")

    with pytest.raises(RunStateError, match="run.json"):
        store.load("r-1")


def test_a_run_json_with_unknown_fields_is_refused(tmp_path):
    store = RunStore(tmp_path)
    store.save(new_state())
    text = (
        store.path("r-1")
        .read_text(encoding="utf-8")
        .replace('"status"', '"surprise": 1, "status"', 1)
    )
    store.path("r-1").write_text(text, encoding="utf-8")

    with pytest.raises(RunStateError, match="surprise"):
        store.load("r-1")


def test_a_missing_run_is_a_clear_error(tmp_path):
    with pytest.raises(RunStateError, match="no run"):
        RunStore(tmp_path).load("r-nobody")


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "", "r 1"])
def test_an_unsafe_run_id_is_refused(tmp_path, run_id):
    with pytest.raises(RunStateError):
        RunStore(tmp_path).path(run_id)


def test_timestamps_must_be_timezone_aware():
    with pytest.raises(ValueError):
        new_state(started_at=datetime(2026, 10, 20, 9, 0))
