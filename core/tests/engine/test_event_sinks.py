"""The run folder is the local sink: every engine event lands in the hash-chained
`events.jsonl` (Zyroflo's chain, never re-made), and other sinks are fed from that chain
in order, once, even after a failure (M1-44)."""

import asyncio
import json
from pathlib import Path

import pytest
from _engine_support import answer

from agentic.engine.gates import Approve, StandaloneAuthorizer
from agentic.telemetry.sinks import ForwardingRecorder, SinkError, forward
from sdlc_commons.gates import self_approval_refusal

PIPELINE = """\
name: test
version: 1.0.0
regime: team
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: designer
  - gate: design-approval
    approver_role: team-lead
"""
SCRIPT = {"ba": [answer("requirements")], "designer": [answer("design")]}


class MemorySink:
    def __init__(self, name: str = "memory", fail: int = 0) -> None:
        self.name, self.fail = name, fail
        self.batches: list[list[dict]] = []

    def deliver(self, run_id, entries) -> None:
        if self.fail:
            self.fail -= 1
            raise ConnectionError("control plane unreachable")
        self.batches.append([dict(e) for e in entries])

    @property
    def received(self) -> list[dict]:
        return [entry for batch in self.batches for entry in batch]


class Spy:
    """The run ledger, noting every event the engine records through it."""

    def __init__(self, ledger) -> None:
        self.ledger, self.recorded = ledger, []

    def __getattr__(self, name):
        return getattr(self.ledger, name)

    def record(self, event, phase="", **fields):
        entry = self.ledger.record(event, phase, **fields)
        self.recorded.append(entry)
        return entry


def run_with(make_harness, sinks, errors=None):
    harness = make_harness(PIPELINE, SCRIPT, gates="zyroflo")
    raw = harness.engine.recorder_for
    made: dict[str, ForwardingRecorder] = {}

    def recorder_for(run_id):
        if run_id not in made:
            on_error = (
                (lambda name, error: errors.append((name, error))) if errors is not None else None
            )
            made[run_id] = ForwardingRecorder(Spy(raw(run_id)), sinks, on_error=on_error)
        return made[run_id]

    harness.engine.recorder_for = recorder_for
    state = asyncio.run(harness.engine.start("test", {"request": "x"}, "dev@acme.example", "r-sk"))
    assert state.pending.id == "design-approval"
    asyncio.run(
        harness.engine.decide(
            "r-sk",
            Approve("design-approval", "lead@acme.example"),
            authorizer=StandaloneAuthorizer(),
            self_approval=self_approval_refusal,
        )
    )
    recorder = made["r-sk"]
    recorder.flush()  # picks up what Zyroflo appended itself (the gate)
    return harness, recorder


def chain(harness) -> list[dict]:
    return harness.ledger("r-sk").events()


def test_every_engine_event_lands_in_events_jsonl_with_a_valid_chain(make_harness):
    harness, recorder = run_with(make_harness, [])
    ledger = harness.ledger("r-sk")
    assert ledger.verify() == []
    hashes = [entry["hash"] for entry in chain(harness)]
    recorded = recorder.ledger.recorded
    assert recorded, "the engine recorded nothing"
    assert [entry["hash"] for entry in recorded] == [
        h for h in hashes if h in {e["hash"] for e in recorded}
    ]
    assert {"step_started", "step_finished", "run_closed"} <= {e["event"] for e in recorded}
    assert "gate_waiting" in {e["event"] for e in chain(harness)}  # written by the gate ledger


def test_a_sink_receives_the_chain_exactly_as_recorded(make_harness):
    sink = MemorySink()
    harness, _ = run_with(make_harness, [sink])
    assert sink.received == chain(harness)  # every entry, in order, with seq/prev/hash
    cursor = json.loads((harness.ledger("r-sk").run_dir / "sinks" / "memory.json").read_text())
    assert cursor["seq"] == len(chain(harness))


def test_a_failing_sink_gets_everything_once_after_recovery(make_harness):  # review focus 2
    sink, errors = MemorySink(fail=3), []
    harness, recorder = run_with(make_harness, [sink], errors)
    assert len(errors) == 3 and all(name == "memory" for name, _ in errors)
    assert harness.ledger("r-sk").verify() == []  # the run went on; the chain is whole
    recorder.flush()
    seqs = [entry["seq"] for entry in sink.received]
    assert seqs == list(range(1, len(chain(harness)) + 1))  # once each, in order


def test_forward_refuses_a_chain_that_does_not_follow_the_cursor(tmp_path: Path):
    entries = [
        {"seq": 1, "prev": "", "hash": "h1"},
        {"seq": 2, "prev": "h1", "hash": "h2"},
    ]
    sink, cursor = MemorySink(), tmp_path / "sinks" / "memory.json"
    assert forward("r", entries, sink, cursor) == 2
    assert forward("r", entries, sink, cursor) == 0  # nothing new: nothing sent
    rewritten = [
        entries[0],
        {"seq": 2, "prev": "h1", "hash": "hX"},
        {"seq": 3, "prev": "hX", "hash": "h3"},
    ]
    with pytest.raises(SinkError, match="does not follow"):
        forward("r", rewritten, sink, cursor)
    assert len(sink.received) == 2


def test_sink_names_must_be_unique_and_simple(make_harness):
    with pytest.raises(ValueError):
        ForwardingRecorder(object(), [MemorySink("a"), MemorySink("a")])
    with pytest.raises(ValueError):
        ForwardingRecorder(object(), [MemorySink("../escape")])
