"""`ForwardingRecorder.screen` (M2-49): an event's fields are screened before the ledger
chains them, so what the sinks later publish is what was chained."""

from __future__ import annotations

from pathlib import Path

from agentic.telemetry.sinks import ForwardingRecorder


class Ledger:
    def __init__(self, folder: Path) -> None:
        self.run_dir = folder
        self.recorded: list[tuple[str, str, dict]] = []

    def record(self, event, phase="", **fields):
        self.recorded.append((event, phase, fields))
        return fields

    def events(self) -> list[dict]:
        return []


def test_recorder_screens_fields_before_the_ledger(tmp_path):
    ledger = Ledger(tmp_path)
    seen: list[tuple[str, dict]] = []

    def screen(event: str, fields: dict) -> dict:
        seen.append((event, fields))
        return {**fields, "outcome": "masked-email"}

    recorder = ForwardingRecorder(ledger, [], screen=screen)
    recorder.record("step_finished", "develop", outcome="leak", attempt=1)
    assert seen == [("step_finished", {"outcome": "leak", "attempt": 1})]
    assert ledger.recorded == [
        ("step_finished", "develop", {"outcome": "masked-email", "attempt": 1})
    ]


def test_without_a_screen_fields_reach_the_ledger_unchanged(tmp_path):
    ledger = Ledger(tmp_path)
    ForwardingRecorder(ledger, []).record("step_started", "develop", attempt=1)
    assert ledger.recorded == [("step_started", "develop", {"attempt": 1})]
