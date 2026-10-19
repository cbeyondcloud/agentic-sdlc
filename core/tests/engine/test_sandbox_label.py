"""The run records which sandbox its commands ran in (M1-33, D38)."""

import asyncio

from _engine_support import answer

ONE_STEP = "name: test\nversion: 1.0.0\nsteps:\n  - id: design\n    agent: designer\n"


def test_the_run_records_its_sandbox(make_harness):
    h = make_harness(ONE_STEP, {"designer": [answer("design")]}, sandbox_label="srt 0.0.78")
    state = asyncio.run(h.engine.start("test", {}, "dev@acme.example", "r-1"))
    assert state.sandbox == "srt 0.0.78"
    assert h.store.load("r-1").sandbox == "srt 0.0.78"


def test_a_run_without_a_sandbox_says_none(make_harness):
    h = make_harness(ONE_STEP, {"designer": [answer("design")]})
    assert asyncio.run(h.engine.start("test", {}, "dev@acme.example", "r-1")).sandbox == "none"
