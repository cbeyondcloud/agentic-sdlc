"""The engine writes one redacted JSON log per step, readable by run and step (M1-47)."""

import asyncio

from _engine_support import answer

from agentic.telemetry.logs import StepLogs, read_logs

PIPELINE = """\
name: test
version: 1.0.0
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: designer
"""
SECRET = "tl-key-0123456789abcdef"  # gitleaks:allow (fake key)


def test_each_step_gets_its_own_json_log(make_harness):
    logs = StepLogs(lambda text: text.replace(SECRET, "<redacted>"))
    harness = make_harness(
        PIPELINE,
        {"ba": [answer("requirements")], "designer": ["not a handoff"]},
        step_logs=logs,
    )
    state = asyncio.run(harness.engine.start("test", {"request": SECRET}, "dev@x", "r-lg"))
    assert state.pending.id == "invalid-output"  # the designer's answer is refused
    run_dir = harness.ledger("r-lg").run_dir

    assert sorted(p.name for p in (run_dir / "logs").iterdir()) == [
        "design-1.jsonl",
        "requirements-1.jsonl",
    ]
    first = read_logs(run_dir, step="requirements")
    assert [r["message"] for r in first] == ["step started", "step ended"]
    assert first[-1]["level"] == "info" and first[-1]["fields"]["outcome"] == "completed"
    assert first[0]["fields"]["agent"] == "ba@1.0.0"
    design = read_logs(run_dir, step="design")
    assert design[-1]["level"] == "warning" and design[-1]["fields"]["outcome"] == "invalid"
    for path in (run_dir / "logs").iterdir():
        assert SECRET not in path.read_text(encoding="utf-8")


def test_without_step_logs_nothing_is_written(make_harness):
    harness = make_harness(
        PIPELINE, {"ba": [answer("requirements")], "designer": [answer("design")]}
    )
    asyncio.run(harness.engine.start("test", {"request": "x"}, "dev@x", "r-nl"))
    assert not (harness.ledger("r-nl").run_dir / "logs").exists()
