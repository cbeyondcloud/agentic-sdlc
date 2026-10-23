"""Runner conformance (M2-41; spec §9.1, §20): one suite, every backend, in named tiers.

Each backend in the capability matrix runs every tier of `conformance.TIERS` against its
scripted stand-in - Zyroflo's `contract` evidence tier (`sdlc_commons.conformance`): the
runner is real, the model is scripted, nothing live is asked (owner decision 73):

  * native      - Pydantic AI with a scripted `FunctionModel`;
  * claude-code - the pinned Claude Code binary (bundled in the SDK) against a TokenLean
                  stub, every other host refused by a proxy;
  * codex       - `fake_codex.py`, which speaks `codex exec --json`;
  * opencode    - the OpenCode ACP profile against `acp_fake_agent.py`, a scripted ACP
                  agent (the pinned opencode binary runs the same cases in
                  test_opencode_contract.py, in the Linux container).

Which tiers apply is read from the matrix (`BackendCapabilities.own_process` for
containment). A failing case fails its test, and the terminal summary names the backend
and tier (core/tests/backends/conftest.py).
"""

import pytest
from conformance import FAIL, NOT_APPLICABLE, PASS, SKIPPED, TIERS, summary_lines, verdicts

from agentic.agents.runner import AgentOutcome
from agentic.backends.capabilities import CAPABILITIES
from agentic.transport import LimitHit

# ── one harness per backend: (make_runner, agent) ─────────────────────────────


def native_harness(request):
    from test_native import make_runner

    resolved = request.getfixturevalue("resolved")
    return make_runner(request.getfixturevalue("tmp_path")), resolved.model_copy(
        update={"tools": []}
    )


def claude_code_harness(request):
    pytest.importorskip("claude_agent_sdk", reason="the claude-code extra is not installed")
    from anthropic_stub import RefusingProxy, Reply, TokenLeanStub
    from test_claude_code_contract import agent_for, runner_for

    tmp_path = request.getfixturevalue("tmp_path")
    stub, proxy = TokenLeanStub(), RefusingProxy()
    request.addfinalizer(stub.close)
    request.addfinalizer(proxy.close)
    tree = tmp_path / "tree"
    tree.mkdir()
    world = (stub, proxy, tree)

    def make(
        agent, *, answers, budget, contract, time_limit_s=None, delay_s=0.0, over_budget=False
    ):
        tokens = 10_000_000 if over_budget else 100
        stub.queue = [Reply(text=a, input_tokens=tokens) for a in answers]
        # Claude Code asks again after an empty answer: a model that says nothing keeps
        # saying nothing (the last scripted answer repeats)
        stub.after = Reply(text=answers[-1], input_tokens=tokens) if answers else None
        stub.delay_s = delay_s
        return runner_for(
            agent, world, tmp_path, budget=budget, contract=contract, time_limit_s=time_limit_s
        )

    return make, agent_for(request.getfixturevalue("resolved"))


def codex_harness(request):
    from test_codex_runner import Fake, agent_for, runner_for

    tmp_path = request.getfixturevalue("tmp_path")
    fake = Fake(tmp_path, request.getfixturevalue("monkeypatch"))

    def make(
        agent, *, answers, budget, contract, time_limit_s=None, delay_s=0.0, over_budget=False
    ):
        tokens = 10_000_000 if over_budget else 100
        usage = {"input_tokens": tokens, "cached_input_tokens": 0, "output_tokens": 20}
        fake.set([{"text": a, "usage": usage, "sleep": delay_s} for a in answers])
        return runner_for(
            agent, tmp_path, budget=budget, contract=contract, time_limit_s=time_limit_s
        )

    return make, agent_for(request.getfixturevalue("resolved"))


def opencode_harness(request):
    pytest.importorskip("acp", reason="the acp extra is not installed")
    from test_acp_runner import World

    from agentic.backends.acp.opencode import OPENCODE

    agent = request.getfixturevalue("resolved").model_copy(
        update={
            "backend": "opencode",
            "model": "openai/contract-model",
            "tools": ["read_files"],
            "temperature": None,
            "effort": None,
        }
    )
    world = World(request.getfixturevalue("tmp_path"), agent)

    def make(
        agent, *, answers, budget, contract, time_limit_s=None, delay_s=0.0, over_budget=False
    ):
        turns = []
        for index, answer in enumerate(answers):
            turn = [{"sleep": delay_s}] if delay_s else []
            turn.append({"say": answer})
            turn.append({"cost": 1000.0 if over_budget else 0.001 * (index + 1)})
            turns.append(turn)
        return world.runner(
            turns, budget=budget, contract=contract, time_limit_s=time_limit_s, profile=OPENCODE
        )

    return make, agent


HARNESSES = {
    "native": native_harness,
    "claude-code": claude_code_harness,
    "codex": codex_harness,
    "opencode": opencode_harness,
}


def test_every_backend_in_the_matrix_has_a_harness():
    assert set(HARNESSES) == set(CAPABILITIES)


def _params():
    for backend in sorted(CAPABILITIES):
        for tier in TIERS:
            for case in tier.cases:
                yield pytest.param(
                    backend,
                    tier,
                    case,
                    id=f"{backend}-{tier.name}-{case.__name__}",
                    marks=pytest.mark.conformance(backend, tier.name),
                )


@pytest.mark.parametrize("backend, tier, case", list(_params()))
def test_conformance(backend, tier, case, request):
    if not tier.applies(CAPABILITIES[backend]):
        pytest.skip(f"{NOT_APPLICABLE}: the capability matrix says {tier.name} does not apply")
    make_runner, agent = HARNESSES[backend](request)
    case(make_runner, agent)


# ── the suite catches a non-conforming runner ─────────────────────────────────


class _Runner:
    """A runner that answers at once, ignoring its time limit and its step scope."""

    def __init__(self, text: str) -> None:
        self.text, self.time_limit_s = text, None

    async def run(self, task) -> AgentOutcome:
        return AgentOutcome(
            status="completed", output=self.text, stopped=None, usage=None, costs=None
        )


def _tier(name: str):
    return next(t for t in TIERS if t.name == name)


def _broken(agent, *, answers, budget, contract, time_limit_s=None, delay_s=0.0, **_):
    return _Runner(answers[0])


def test_a_runner_ignoring_its_time_limit_fails_the_limits_tier():
    time_case = _tier("limits").cases[1]
    with pytest.raises(AssertionError):
        time_case(_broken, agent=None)


def test_a_runner_that_never_adopts_fails_containment():
    [contained] = _tier("containment").cases
    with pytest.raises(AssertionError, match="never adopted"):
        contained(_broken, agent=None)


def test_a_runner_that_never_charges_fails_the_budget_case():
    budget_case = _tier("limits").cases[0]
    with pytest.raises(pytest.fail.Exception, match=LimitHit.__name__):
        budget_case(_broken, agent=None)


def test_a_runner_completing_on_an_empty_answer_fails_the_output_tier():
    empty_case = _tier("output").cases[-1]
    with pytest.raises(AssertionError, match="said nothing"):
        empty_case(_broken, agent=None)


def test_the_summary_names_each_backend_and_tier():
    table = verdicts(
        [
            ("codex", "output", PASS),
            ("codex", "limits", PASS),
            ("codex", "limits", FAIL),
            ("native", "output", PASS),
            ("native", "containment", NOT_APPLICABLE),
            ("opencode", "output", SKIPPED),
        ]
    )
    assert table == {
        "codex": {"output": PASS, "limits": FAIL},
        "native": {"output": PASS, "containment": NOT_APPLICABLE},
        "opencode": {"output": SKIPPED},
    }
    lines = summary_lines(table, "contract", "scripted stand-ins")
    assert lines[0] == "runner conformance - evidence tier: contract (scripted stand-ins)"
    assert any(line.split()[:3] == ["codex", "pass", "FAIL"] for line in lines)
    assert any(line.split()[:4] == ["native", "pass", "-", "n/a"] for line in lines)
    assert lines[-1] == "  FAILING TIERS: codex/limits"
