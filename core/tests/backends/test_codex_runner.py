"""Tests for the codex runner and backend (M2-40) with a stand-in `codex` (fake_codex.py),
so they run on every OS: the backend conformance, charging from `turn.completed.usage`
(DF-19), the contract retry in a new exec, the turn and time limits, and the refusals
(no sandbox, native Windows, another Codex version)."""

import asyncio
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest
from conformance import CASES, TASK, FakeContract, slice_

import agentic.backends as backends
from agentic.agents.runner import ResumeError
from agentic.backends.codex import (
    CODEX_VERSION,
    CodexBackend,
    CodexError,
    CodexRunner,
    CodexSettings,
    lock_down,
)
from agentic.backends.codex_rules import ShellRules
from agentic.engine.engine import StepContext
from agentic.policy.model import ModelPrice
from agentic.transport import CallIdentity, LimitHit

FAKE = Path(__file__).with_name("fake_codex.py")
KEY = "tl-" + "test-" + "key"
PRICE = ModelPrice(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15"))
IDENTITY = CallIdentity(
    team="payments",
    user="dev@acme.example",
    repo="acme/app",
    pipeline="feature-delivery",
    run_id="r-1",
    step="develop",
    agent="developer@1.2.0",
    policy_version="v1",
)


class Fake:
    """One test's fake codex: its plan (the answers in order) and what each start saw."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.plan = tmp_path / "plan.json"
        self.log = tmp_path / "codex-log.jsonl"
        self.set([])
        monkeypatch.setenv("FAKE_CODEX_PLAN", str(self.plan))

    def set(self, answers: list[dict]) -> None:
        self.plan.write_text(json.dumps({"answers": answers, "log": str(self.log)}), "utf-8")

    def starts(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]


@pytest.fixture
def fake(tmp_path, monkeypatch) -> Fake:
    return Fake(tmp_path, monkeypatch)


def agent_for(resolved, **changes):
    update = {"backend": "codex", "tools": ["read_files", "run_shell"], "temperature": None}
    return resolved.model_copy(update={**update, "effort": None, **changes})


def runner_for(
    agent, tmp_path, *, budget=None, contract=None, time_limit_s=None, wrap=None, sandbox=False
):
    budget = budget or slice_()
    settings = CodexSettings(
        gateway_url="http://127.0.0.1:9",
        api_key=KEY,
        home_root=tmp_path / "homes",
        cli=(sys.executable, str(FAKE)),
        require_sandbox=sandbox,
    )
    tree = tmp_path / "tree"
    tree.mkdir(exist_ok=True)
    step = StepContext("r-1", "develop", 1, agent, budget, contract)
    launch = lock_down(
        step,
        settings=settings,
        identity=IDENTITY,
        rules=ShellRules.of(agent),
        cwd=tree,
        srt_boundary=bool(wrap),
        environ=dict(os.environ),
    )
    return CodexRunner(
        agent,
        launch=launch,
        budget=budget,
        price_for=lambda model: PRICE,
        contract=contract,
        time_limit_s=time_limit_s,
        wrap=wrap,
        require_sandbox=sandbox,
    )


def run(runner):
    return asyncio.run(runner.run(TASK))


# ── the conformance every backend passes ──────────────────────────────────────


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.__name__)
def test_codex_conformance(case, resolved, fake, tmp_path):
    def make(
        agent, *, answers, budget, contract, time_limit_s=None, delay_s=0.0, over_budget=False
    ):
        tokens = 10_000_000 if over_budget else 100
        usage = {"input_tokens": tokens, "cached_input_tokens": 0, "output_tokens": 20}
        fake.set([{"text": a, "usage": usage, "sleep": delay_s} for a in answers])
        return runner_for(
            agent, tmp_path, budget=budget, contract=contract, time_limit_s=time_limit_s
        )

    case(make, agent_for(resolved))


# ── charging (DF-19) ──────────────────────────────────────────────────────────


def test_turn_usage_is_charged_estimated_at_policy_price(resolved, fake, tmp_path):
    usage = {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 40}
    fake.set([{"text": "VALID", "usage": usage, "tools": 2}])
    budget = slice_()
    outcome = run(runner_for(agent_for(resolved), tmp_path, budget=budget))

    expected = (Decimal(1200) * 3 + Decimal(40) * 15) / Decimal(1_000_000)  # no cache price
    assert budget.spent() == expected
    cost, charged = outcome.costs.calls[0]
    assert (cost.source, charged, cost.tokens_cache_read) == ("estimated", expected, 800)
    assert "codex" in cost.reason
    assert outcome.usage.input_tokens == 1200 and outcome.usage.output_tokens == 40
    assert outcome.usage.requests == 3  # one model call per tool result, plus the answer


def test_unpriced_model_uses_the_fallback_estimate_per_call(resolved, fake, tmp_path):
    fake.set([{"text": "VALID", "tools": 1}])
    runner = runner_for(agent_for(resolved), tmp_path)
    runner.price_for = lambda model: None
    outcome = run(runner)
    cost, charged = outcome.costs.calls[0]
    assert (cost.source, charged) == ("unavailable", Decimal("0.02"))


def test_a_turn_without_usage_charges_nothing_but_completes(resolved, fake, tmp_path):
    fake.set([{"text": "VALID", "usage": None}])
    budget = slice_()
    outcome = run(runner_for(agent_for(resolved), tmp_path, budget=budget))
    assert outcome.status == "completed" and budget.spent() == 0


def test_an_exec_killed_at_the_time_limit_is_still_charged(resolved, fake, tmp_path):
    # review I3: its model calls happened, but Codex reports usage only at turn end
    fake.set([{"text": "VALID late", "tools": 3, "sleep": 30}])
    budget = slice_()
    outcome = run(runner_for(agent_for(resolved), tmp_path, budget=budget, time_limit_s=3))
    assert (outcome.status, outcome.stopped) == ("stopped", "time")
    [(cost, charged)] = outcome.costs.calls
    assert (cost.source, charged) == ("unavailable", Decimal("0.04"))  # 3 tool results + 1
    assert "before turn.completed" in cost.reason
    assert budget.spent() == Decimal("0.04") and outcome.usage.requests == 4


def test_an_exec_killed_at_the_turn_limit_is_still_charged(resolved, fake, tmp_path):
    agent = agent_for(resolved)
    agent = agent.model_copy(update={"limits": agent.limits.model_copy(update={"max_turns": 2})})
    fake.set([{"text": "VALID", "tools": 5, "tool_sleep": 0.2}])
    budget = slice_()
    outcome = run(runner_for(agent, tmp_path, budget=budget))
    assert (outcome.status, outcome.stopped) == ("stopped", "turns")
    [(cost, charged)] = outcome.costs.calls
    assert (cost.source, charged) == ("unavailable", Decimal("0.03"))  # 2 tool results + 1
    assert budget.spent() == Decimal("0.03") and outcome.usage.requests == 3


def test_a_failed_turn_is_still_charged(resolved, fake, tmp_path):
    fake.set([{"fail": "stream closed before response.completed", "tools": 1}])
    budget = slice_()
    runner = runner_for(agent_for(resolved), tmp_path, budget=budget)
    with pytest.raises(RuntimeError, match="stream closed"):
        run(runner)
    [(cost, charged)] = runner.costs.calls
    assert (cost.source, charged) == ("unavailable", Decimal("0.02"))
    assert budget.spent() == Decimal("0.02") and runner.usage.requests == 2


def test_codex_refusing_to_start_charges_nothing(resolved, fake, tmp_path):
    fake.set([{"early": "Error loading config.toml: unknown configuration field"}])
    budget = slice_()
    runner = runner_for(agent_for(resolved), tmp_path, budget=budget)
    with pytest.raises(RuntimeError, match="unknown configuration field"):
        run(runner)
    assert runner.costs.calls == [] and budget.spent() == 0


def test_a_charge_killed_past_the_slice_raises_limit_hit(resolved, fake, tmp_path):
    fake.set([{"text": "VALID late", "tools": 3, "sleep": 30}])
    runner = runner_for(agent_for(resolved), tmp_path, budget=slice_("0.01"), time_limit_s=3)
    with pytest.raises(LimitHit):
        run(runner)
    assert runner.costs.calls  # the calls happened: they are recorded


def test_usage_past_the_slice_raises_limit_hit_after_recording(resolved, fake, tmp_path):
    usage = {"input_tokens": 10_000_000, "cached_input_tokens": 0, "output_tokens": 0}
    fake.set([{"text": "VALID", "usage": usage}])
    runner = runner_for(agent_for(resolved), tmp_path, budget=slice_("0.01"))
    with pytest.raises(LimitHit):
        run(runner)
    assert runner.costs.calls  # the call happened: it is recorded


# ── the answer ────────────────────────────────────────────────────────────────


def test_schema_envelope_is_unwrapped_and_checked(resolved, fake, tmp_path):
    fake.set([{"text": json.dumps({"document": "VALID in an envelope"})}])
    outcome = run(runner_for(agent_for(resolved), tmp_path, contract=FakeContract()))
    assert (outcome.status, outcome.output) == ("completed", "VALID in an envelope")
    [start] = fake.starts()
    assert "--output-schema" in start["argv"]


def test_invalid_answer_retried_once_in_a_new_exec(resolved, fake, tmp_path):
    fake.set([{"text": "prose"}, {"text": "VALID second"}])
    outcome = run(runner_for(agent_for(resolved), tmp_path, contract=FakeContract()))
    assert (outcome.status, outcome.output) == ("completed", "VALID second")
    first, second = fake.starts()
    assert first["prompt"] == TASK.prompt
    assert TASK.prompt in second["prompt"] and "prose" in second["prompt"]
    assert "it must start with VALID" in second["prompt"]


def test_developer_instructions_reach_the_config(resolved, fake, tmp_path):
    fake.set([{"text": "VALID"}])
    runner = runner_for(agent_for(resolved), tmp_path, contract=FakeContract())
    run(runner)
    config = (runner.launch.home / "config.toml").read_text(encoding="utf-8")
    assert "You are the Developer." in config and "document" in config


def test_a_transient_error_in_a_completed_turn_is_not_a_failure(resolved, fake, tmp_path):
    fake.set([{"text": "VALID", "warn": "stream disconnected; reconnecting"}])
    assert run(runner_for(agent_for(resolved), tmp_path)).status == "completed"


def test_codex_home_is_rewritten_before_the_retry_exec(resolved, fake, tmp_path):
    fake.set([{"text": "prose", "tamper": True}, {"text": "VALID second"}])
    outcome = run(runner_for(agent_for(resolved), tmp_path, contract=FakeContract()))
    assert outcome.status == "completed"
    first, second = fake.starts()
    assert first["rules"] == second["rules"] and '"allow"' not in second["rules"]


def test_turn_failed_raises_with_the_reason(resolved, fake, tmp_path):
    fake.set([{"fail": "stream closed before response.completed"}])
    with pytest.raises(RuntimeError, match="stream closed"):
        run(runner_for(agent_for(resolved), tmp_path))


def test_a_cli_that_cannot_start_raises_codex_error(resolved, fake, tmp_path):
    runner = runner_for(agent_for(resolved), tmp_path)
    runner.launch.argv[0] = str(tmp_path / "no-such-codex")
    with pytest.raises(CodexError, match="did not start"):
        run(runner)


# ── limits ────────────────────────────────────────────────────────────────────


def test_turn_limit_stops(resolved, fake, tmp_path):
    agent = agent_for(resolved)
    agent = agent.model_copy(update={"limits": agent.limits.model_copy(update={"max_turns": 2})})
    fake.set([{"text": "VALID", "tools": 5, "tool_sleep": 0.2}])
    outcome = run(runner_for(agent, tmp_path))
    assert (outcome.status, outcome.stopped) == ("stopped", "turns")


def test_time_limit_kills_the_tree(resolved, fake, tmp_path):
    fake.set([{"text": "VALID late", "sleep": 30, "child": True}])
    started = time.monotonic()
    outcome = run(runner_for(agent_for(resolved), tmp_path, time_limit_s=3))
    assert (outcome.status, outcome.stopped) == ("stopped", "time")
    assert time.monotonic() - started < 20
    child = int(fake.log.with_suffix(".child").read_text())
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(child)


# ── refusals ──────────────────────────────────────────────────────────────────


def test_runner_refuses_without_a_sandbox(resolved, fake, tmp_path):  # Review Focus 2
    runner = runner_for(agent_for(resolved), tmp_path, sandbox=True, wrap=None)
    with pytest.raises(CodexError, match="sandbox"):
        run(runner)
    assert fake.starts() == []


def test_resume_is_refused(resolved, fake, tmp_path):
    runner = runner_for(agent_for(resolved), tmp_path)
    with pytest.raises(ResumeError, match="never pause"):
        asyncio.run(runner.resume(Path("x"), {}, "sha256:0"))


def backend_for(tmp_path, *, version=CODEX_VERSION, wrap=None, require_sandbox=False):
    settings = CodexSettings(
        gateway_url="http://127.0.0.1:9",
        api_key=KEY,
        home_root=tmp_path / "homes",
        cli=(sys.executable, str(FAKE)),
        require_sandbox=require_sandbox,
    )
    return CodexBackend(
        settings,
        identity_for=lambda step: IDENTITY,
        prices=lambda model: PRICE,
        wrap_for=lambda step: wrap,
        version=lambda cli: version,
    )


def test_backend_builds_a_runner_over_the_step_tree(resolved, tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.backends.codex.native_windows", lambda: False)
    step = StepContext("r-1", "develop", 1, agent_for(resolved), slice_(), None)
    runner = backend_for(tmp_path).runner(
        step, tool_context=None, state_dir=tmp_path / "state", spawner=None
    )
    assert isinstance(runner, CodexRunner)
    argv = runner.launch.argv
    assert argv[argv.index("-C") + 1] == (tmp_path / "state").resolve().as_posix()


def test_native_windows_is_refused(resolved, tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.backends.codex.native_windows", lambda: True)
    step = StepContext("r-1", "develop", 1, agent_for(resolved), slice_(), None)
    with pytest.raises(CodexError, match="WSL2"):
        backend_for(tmp_path).runner(step, tool_context=None, state_dir=tmp_path, spawner=None)


def test_another_codex_version_is_refused(resolved, tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.backends.codex.native_windows", lambda: False)

    def wrong(cli):
        raise CodexError("the Codex CLI is version 0.1.0")

    step = StepContext("r-1", "develop", 1, agent_for(resolved), slice_(), None)
    backend = backend_for(tmp_path)
    backend.version = wrong
    with pytest.raises(CodexError, match="0.1.0"):
        backend.runner(step, tool_context=None, state_dir=tmp_path, spawner=None)


def test_available_needs_extra_cli_and_posix(monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda name: "/usr/local/bin/codex")
    monkeypatch.setattr(backends.os, "name", "posix")
    assert "codex" in backends.available()
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    assert "codex" not in backends.available()
    monkeypatch.setattr(backends.shutil, "which", lambda name: "C:/codex.exe")
    monkeypatch.setattr(backends.os, "name", "nt")
    assert "codex" not in backends.available()


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=False
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie child of ours counts as gone (killed, not yet reaped by its parent)
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return True
