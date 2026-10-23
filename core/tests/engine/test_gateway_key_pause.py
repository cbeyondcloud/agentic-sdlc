"""A revoked gateway key pauses the run (M2-17, ruling P11): the pause says to log in with a
new key, and `resume` continues once that is done - nothing is failed or lost."""

import asyncio

from _engine_support import answer

from agentic.transport import GatewayKeyRejected

ONE_STEP = """\
name: test
version: 1.0.0
steps:
  - id: requirements
    agent: ba
"""


class _Rejected:
    """A runner whose every model call meets a revoked key."""

    async def run(self, task):
        raise GatewayKeyRejected()

    async def resume(self, state_path, decisions, expected_digest):
        raise GatewayKeyRejected()


def test_a_rejected_gateway_key_pauses_the_run_until_a_new_login(make_harness):
    harness = make_harness(ONE_STEP, {"ba": [answer("requirements")]})
    scripted = harness.engine.runner_factory
    harness.engine.runner_factory = lambda ctx: _Rejected()
    state = asyncio.run(harness.engine.start("test", {}, "dev@acme.example", "r-1"))
    assert (state.status, state.pending.id) == ("paused", "gateway-key")
    assert "agentic login" in state.pending.reason
    assert state.history[-1].outcome == "failed"

    harness.engine.runner_factory = scripted  # the person logged in with a new key
    assert asyncio.run(harness.engine.resume("r-1")).status == "completed"
