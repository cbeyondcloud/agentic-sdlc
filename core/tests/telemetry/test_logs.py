"""Structured per-step logs: JSON lines, levels, secrets redacted, readable by run and
step (M1-47)."""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from agentic.telemetry.logs import StepLogs, read_logs, render_logs
from sdlc_commons.redaction import redactor

SECRET = "sk-live-9f8e7d6c5b4a39281706"
LOG = logging.getLogger("agentic.test")


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_planted_secret_never_reaches_the_log(tmp_path: Path):  # review focus 3
    logs = StepLogs(redactor(env={"ACME_API_KEY": SECRET}))
    with logs.step(tmp_path, "r-1", "develop", 1):
        LOG.info("calling with key %s", SECRET)
        LOG.warning("retrying", extra={"fields": {"header": f"Bearer {SECRET}", "n": 2}})
        try:
            raise RuntimeError(f"401 for key {SECRET}")
        except RuntimeError:
            LOG.exception("call failed")
    text = (tmp_path / "logs" / "develop-1.jsonl").read_text(encoding="utf-8")
    assert SECRET not in text
    records = lines(tmp_path / "logs" / "develop-1.jsonl")
    assert [r["level"] for r in records] == ["info", "warning", "error"]
    assert "<redacted>" in records[0]["message"]
    assert records[1]["fields"]["n"] == 2 and "<redacted>" in records[1]["fields"]["header"]
    assert "RuntimeError" in records[2]["error"] and "<redacted>" in records[2]["error"]


def test_a_log_needs_a_redactor():
    with pytest.raises(TypeError):
        StepLogs(None)


def test_a_failing_redactor_writes_no_raw_text(tmp_path: Path):
    def broken(text: str) -> str:
        raise ValueError("boom")

    with StepLogs(broken).step(tmp_path, "r-1", "s", 1):
        LOG.info("secret %s", SECRET)
    assert SECRET not in (tmp_path / "logs" / "s-1.jsonl").read_text(encoding="utf-8")


def test_level_filter_drops_debug(tmp_path: Path):
    with StepLogs(lambda text: text, level="info").step(tmp_path, "r-1", "s", 1):
        LOG.debug("noise")
        LOG.info("kept")
    assert [r["message"] for r in lines(tmp_path / "logs" / "s-1.jsonl")] == ["kept"]


def test_records_outside_a_step_are_not_written(tmp_path: Path):
    logs = StepLogs(lambda text: text)
    LOG.warning("before")
    with logs.step(tmp_path, "r-1", "s", 1):
        LOG.warning("during")
    LOG.warning("after")
    assert [r["message"] for r in lines(tmp_path / "logs" / "s-1.jsonl")] == ["during"]


def test_read_logs_by_step_and_iteration(tmp_path: Path):
    logs = StepLogs(lambda text: text, level="debug")
    for step, iteration, level in [
        ("develop", 1, "info"),
        ("review", 1, "warning"),
        ("develop", 2, "debug"),
        ("phase-1", 1, "error"),
    ]:
        with logs.step(tmp_path, "r-1", step, iteration):
            LOG.log(logging.getLevelName(level.upper()), f"{step} #{iteration}")

    assert [r["message"] for r in read_logs(tmp_path)] == [
        "develop #1",
        "review #1",
        "develop #2",
        "phase-1 #1",
    ]
    assert [r["message"] for r in read_logs(tmp_path, step="develop")] == [
        "develop #1",
        "develop #2",
    ]
    assert [r["message"] for r in read_logs(tmp_path, step="develop-2")] == ["develop #2"]
    assert [r["message"] for r in read_logs(tmp_path, step="phase-1")] == ["phase-1 #1"]
    assert [r["message"] for r in read_logs(tmp_path, min_level="warning")] == [
        "review #1",
        "phase-1 #1",
    ]
    rendered = render_logs(read_logs(tmp_path, step="review"))
    assert "WARNING" in rendered and "review-1" in rendered and "review #1" in rendered
    assert read_logs(tmp_path / "no-such-run") == []


def test_two_runs_in_one_process_keep_their_logs_apart(tmp_path: Path):  # review focus 6
    logs = StepLogs(lambda text: text)

    async def work(run: str):
        with logs.step(tmp_path / run, run, "develop", 1):
            for n in range(3):
                LOG.info(f"{run} line {n}")
                await asyncio.sleep(0)

    async def both():
        await asyncio.gather(work("r-a"), work("r-b"))

    asyncio.run(both())
    for run in ("r-a", "r-b"):
        messages = [r["message"] for r in read_logs(tmp_path / run)]
        assert messages == [f"{run} line {n}" for n in range(3)]
