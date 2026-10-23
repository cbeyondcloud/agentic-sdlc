"""The shared ACP runner (M2-57; spike S7) against a scripted ACP agent run as a real
subprocess over stdio (`acp_fake_agent.py`): the prompt, the result, the permission gate,
the per-step home, auth and mode, and every cost path (the conformance tiers run on this
agent in test_conformance.py). No agent binary is needed, so these run on Windows, macOS
and Linux."""

import asyncio
import json
import math
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from conformance import TASK, FakeContract, slice_
from test_processes import gone

from agentic.agents.runner import AgentTask, ResumeError
from agentic.backends.acp.profile import AcpProfile, Launch
from agentic.engine.engine import StepContext
from agentic.policy.model import ModelPrice
from agentic.transport import CallIdentity, LimitHit

pytest.importorskip("acp")

from agentic.backends.acp.runner import (  # noqa: E402 - needs the acp extra
    AcpBackend,
    AcpSettings,
    resolve_command,
)

FAKE = Path(__file__).with_name("acp_fake_agent.py")
GATEWAY = "https://tokenlean.acme.internal"
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
PRICE = ModelPrice(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15"))


def fake_profile(**changes) -> AcpProfile:
    fields = {
        "name": "fake",
        "command": ("fake-agent", "acp"),  # replaced by the settings' command below
        "version": "0",
        "configure": lambda setup: Launch(env={"FAKE_ROUTE": setup.gateway_url}),
        "quiet": {"FAKE_QUIET": "1"},
        "inherit": ("FAKE_INHERITED", "OPENAI_API_KEY"),  # a secret name is never inherited
        "session_cost": True,
        **changes,
    }
    return AcpProfile(**fields)


class World:
    """One step's tree, the agent's script and log, and a runner factory."""

    def __init__(self, tmp_path: Path, agent) -> None:
        self.tmp, self.agent = tmp_path, agent
        self.tree = tmp_path / "tree"
        self.tree.mkdir()
        self.log = tmp_path / "agent-log.jsonl"
        self.home_root = tmp_path / "acp-home"

    def runner(
        self,
        turns,
        *,
        budget=None,
        contract=None,
        time_limit_s=None,
        profile=None,
        price=PRICE,
        agent=None,
        **script,
    ):
        path = self.tmp / "script.json"
        path.write_text(json.dumps({"log": str(self.log), "turns": turns, **script}), "utf-8")
        settings = AcpSettings(
            gateway_url=GATEWAY,
            api_key="tl-key",
            home_root=self.home_root,
            command=(sys.executable, str(FAKE), str(path)),
        )
        backend = AcpBackend(
            profile or fake_profile(),
            settings,
            identity_for=lambda step: IDENTITY,
            prices=lambda model: price,
            fallback_estimate_usd=Decimal("0.01"),
            time_limit_s=time_limit_s,
            unsandboxed=True,  # a scripted agent on the host: no shell, nothing to contain
        )
        step = StepContext("r-1", "develop", 1, agent or self.agent, budget or slice_(), contract)
        return backend.runner(step, tool_context=None, state_dir=self.tree, spawner=None)

    def events(self, name: str) -> list[dict]:
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [e for e in map(json.loads, lines) if e["event"] == name]


@pytest.fixture
def agent(resolved):
    return resolved.model_copy(
        update={"backend": "fake", "tools": ["read_files"], "temperature": None, "effort": None}
    )


@pytest.fixture
def world(tmp_path, agent):
    return World(tmp_path, agent)


def run(runner, task: AgentTask = TASK):
    return asyncio.run(runner.run(task))


# ── prompt and result ─────────────────────────────────────────────────────────


def test_the_first_prompt_carries_instructions_inputs_and_contract(world):
    task = AgentTask(
        instructions="You are the Developer.",
        inputs={"design": "Build the parser."},
        prompt="Write the report.",
    )
    run(world.runner([[{"say": "VALID"}]], contract=FakeContract()), task)
    (prompt,) = world.events("prompt")
    assert prompt["blocks"] == 2
    for part in ("You are the Developer.", "Build the parser.", "<handoff", "starting VALID"):
        assert part in prompt["text"], part
    assert prompt["text"].rstrip().endswith("Write the report.")


def test_the_result_is_the_text_after_the_last_tool_call(world):
    turn = [
        {"say": "I will run the tests first."},
        {"tool": "bash", "command": "pytest"},
        {"say": "VALID "},
        {"say": "final"},
    ]
    outcome = run(world.runner([turn], contract=FakeContract()))
    assert (outcome.status, outcome.output) == ("completed", "VALID final")


def test_a_retry_goes_back_in_the_same_session_with_the_errors(world):
    outcome = run(world.runner([[{"say": "prose"}], [{"say": "VALID"}]], contract=FakeContract()))
    assert outcome.status == "completed"
    retry = world.events("prompt")[1]["text"]
    assert "it must start with VALID" in retry
    assert len(world.events("new_session")) == 1


# ── the second gate ───────────────────────────────────────────────────────────


def test_every_permission_request_is_rejected_once(world):
    turn = [
        {"tool": "bash", "command": "touch made-by-agent.txt", "ask": True},
        {"tool": "bash", "command": "echo read-only", "ask": True},
        {"say": "VALID done"},
    ]
    outcome = run(world.runner([turn]))
    answers = world.events("permission")
    assert [a["outcome"] for a in answers] == [{"outcome": "selected", "optionId": "reject"}] * 2
    assert outcome.status == "completed"
    # the program only: a command's arguments can carry a credential (as in the guard)
    assert "execute: touch" in outcome.detail and "execute: echo" in outcome.detail
    assert "made-by-agent.txt" not in outcome.detail and "read-only" not in outcome.detail


def test_with_no_reject_option_a_request_is_cancelled_never_allowed(world):
    turn = [{"tool": "bash", "command": "rm -rf x", "ask": True, "options": "no_reject"}]
    run(world.runner([turn + [{"say": "VALID"}]]))
    (answer,) = world.events("permission")
    assert answer["outcome"] == {"outcome": "cancelled"}


# ── launch: home, environment, auth, mode ─────────────────────────────────────


def test_the_agent_runs_in_the_tree_with_a_home_of_its_own(world, monkeypatch):
    monkeypatch.setenv("FAKE_INHERITED", "corp-ca.pem")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "y" * 24)
    run(world.runner([[{"say": "VALID"}]]))
    (init,) = world.events("initialize")
    home = world.home_root / "r-1" / "develop-1" / "developer"
    env = init["env"]
    assert Path(env["HOME"]) == home and Path(env["USERPROFILE"]) == home
    assert Path(env["XDG_CONFIG_HOME"]).is_relative_to(home)
    assert env["FAKE_QUIET"] == "1" and env["FAKE_ROUTE"] == GATEWAY
    assert env["FAKE_INHERITED"] == "corp-ca.pem"
    assert env["OPENAI_API_KEY"] is None
    assert Path(world.events("cwd")[0]["cwd"]).resolve() == world.tree.resolve()
    assert Path(world.events("new_session")[0]["cwd"]) == world.tree
    assert world.events("new_session")[0]["mcp_servers"] == 0


def test_what_the_agent_started_does_not_outlive_the_step(world):
    run(world.runner([[{"spawn": True}, {"say": "VALID"}]]))
    (spawned,) = world.events("spawned")
    assert gone(spawned["pid"])


def test_the_step_home_is_thrown_away_with_the_step(world):
    run(world.runner([[{"say": "VALID"}]]))
    assert world.events("initialize")
    assert not (world.home_root / "r-1" / "develop-1" / "developer").exists()


def test_a_secret_file_is_gone_once_the_session_is_open(world):  # review F1
    # the agent read it while it loaded its config; its shell commands must not find it
    profile = fake_profile(
        configure=lambda setup: Launch(files={"secret.key": setup.api_key}),
        secret_files=("secret.key",),
    )
    turns = [[{"say": "prose"}], [{"say": "VALID"}]]
    outcome = run(
        world.runner(turns, profile=profile, contract=FakeContract(), watch=["secret.key"])
    )
    assert outcome.status == "completed"
    assert world.events("new_session")[0]["watched"] == {"secret.key": True}
    assert [e["watched"] for e in world.events("prompt")] == [{"secret.key": False}] * 2


def test_the_client_offers_the_agent_no_file_or_terminal_access(world):
    run(world.runner([[{"say": "VALID"}]]))
    (init,) = world.events("initialize")
    assert init["protocol"] == 1
    assert init["capabilities"]["fs"] == {"readTextFile": False, "writeTextFile": False}
    assert init["capabilities"]["terminal"] is False


def test_auth_and_mode_are_set_when_the_profile_names_them(world):
    profile = fake_profile(auth_method="api-key", mode="build")
    turns = [[{"say": "VALID"}]]
    run(world.runner(turns, profile=profile, auth_methods=["api-key"], modes=["default", "build"]))
    assert [e["method"] for e in world.events("authenticate")] == ["api-key"]
    assert [e["mode"] for e in world.events("set_mode")] == ["build"]


def test_without_them_neither_is_called(world):
    run(world.runner([[{"say": "VALID"}]], auth_methods=["api-key"], modes=["default"]))
    assert world.events("authenticate") == [] and world.events("set_mode") == []


def test_a_tree_the_profile_refuses_never_starts_the_agent(world):
    profile = fake_profile(refuse_tree=lambda tree: [f"{tree.name}/.agent/plugins/x.js"])
    with pytest.raises(RuntimeError, match=r"refuses .*\.agent/plugins/x\.js"):
        run(world.runner([[{"say": "VALID"}]], profile=profile))
    assert not world.log.exists()


def test_an_agent_that_fails_to_start_a_session_names_its_stderr(world, tmp_path):
    crash = tmp_path / "crash.py"
    crash.write_text("import sys\nsys.stderr.write('boom: no config dir')\nsys.exit(3)\n", "utf-8")
    runner = world.runner([])
    runner.command = [sys.executable, str(crash)]
    with pytest.raises(RuntimeError, match="boom: no config dir"):
        run(runner)


def test_an_agent_on_another_protocol_version_is_refused(world):
    with pytest.raises(RuntimeError, match="protocol"):
        run(world.runner([[{"say": "VALID"}]], protocol=2))


# ── how a turn ends ───────────────────────────────────────────────────────────


def test_the_agent_s_turn_limit_ends_the_step_stopped(world):
    outcome = run(world.runner([[{"say": "partial"}, {"stop": "max_turn_requests"}]]))
    assert (outcome.status, outcome.stopped) == ("stopped", "turns")


@pytest.mark.parametrize("reason", ["refusal", "cancelled"])
def test_a_refused_or_cancelled_turn_is_an_error(world, reason):
    with pytest.raises(RuntimeError, match=reason):
        run(world.runner([[{"say": "no"}, {"stop": reason}]]))


def test_resume_is_refused(world):
    runner = world.runner([])
    with pytest.raises(ResumeError, match="never pause"):
        asyncio.run(runner.resume(Path("x"), {}, "sha256:0"))


# ── cost ──────────────────────────────────────────────────────────────────────


def test_the_agent_s_session_cost_at_policy_prices_is_charged_by_difference(world):
    turns = [[{"say": "prose"}, {"cost": 0.002}], [{"say": "VALID"}, {"cost": 0.005}]]
    budget = slice_()
    outcome = run(world.runner(turns, budget=budget, contract=FakeContract()))
    assert budget.spent() == Decimal("0.005")
    charged = [amount for _, amount in outcome.costs.calls]
    assert charged == [Decimal("0.002"), Decimal("0.003")]
    assert {cost.source for cost, _ in outcome.costs.calls} == {"estimated"}


def test_without_a_session_cost_the_turn_s_usage_is_priced(world):
    turn = [{"say": "VALID"}, {"usage": {"input": 1000, "output": 200}}]
    budget = slice_()
    outcome = run(world.runner([turn], budget=budget, profile=fake_profile(session_cost=False)))
    expected = (Decimal(1000) * 3 + Decimal(200) * 15) / Decimal(1_000_000)
    assert budget.spent() == expected
    assert (outcome.usage.input_tokens, outcome.usage.output_tokens) == (1000, 200)


def test_a_turn_charged_from_usage_is_not_charged_again_by_the_next_session_cost(world):
    turns = [
        [{"say": "prose"}, {"usage": {"input": 1000, "output": 0}}],  # no session cost
        [{"say": "VALID"}, {"cost": 0.005}],  # the session so far, the first turn included
    ]
    budget = slice_()
    run(world.runner(turns, budget=budget, contract=FakeContract()))
    assert budget.spent() == Decimal("0.005")


def test_a_session_cost_from_a_profile_without_our_prices_is_ignored(world):
    turn = [{"say": "VALID"}, {"cost": 9.99}, {"usage": {"input": 1000, "output": 0}}]
    budget = slice_()
    run(world.runner([turn], budget=budget, profile=fake_profile(session_cost=False)))
    assert budget.spent() == Decimal(3000) / Decimal(1_000_000)


def test_with_no_usage_at_all_the_cost_is_estimated_from_text_length(world):
    turn = [{"say": "VALID done"}]
    budget = slice_()
    outcome = run(world.runner([turn], budget=budget, profile=fake_profile(session_cost=False)))
    ((cost, charged),) = outcome.costs.calls
    assert cost.source == "estimated" and "text length" in cost.reason
    assert cost.tokens_out == math.ceil(len("VALID done") / 4)
    assert cost.tokens_in > 0 and charged == budget.spent() > 0


def test_an_unpriced_model_is_charged_the_fallback_per_model_call(world):
    turn = [{"tool": "bash", "command": "pytest"}, {"say": "VALID"}, {"cost": 0.0}]
    budget = slice_()
    outcome = run(world.runner([turn], budget=budget, price=None))
    ((cost, charged),) = outcome.costs.calls
    assert cost.source == "unavailable"
    assert charged == budget.spent() == Decimal("0.02")  # two model calls: tool, answer


def test_a_charge_past_the_slice_raises_and_stays_in_the_log(world):
    runner = world.runner([[{"say": "VALID"}, {"cost": 50.0}]], budget=slice_("0.01"))
    with pytest.raises(LimitHit):
        run(runner)
    assert [amount for _, amount in runner.costs.calls] == [Decimal("50")]


# ── launching the command ─────────────────────────────────────────────────────


def test_the_agent_command_is_resolved_on_path():
    assert resolve_command(("agent", "acp"), which=lambda name: "/usr/bin/agent") == [
        "/usr/bin/agent",
        "acp",
    ]


def test_a_missing_agent_command_is_a_clear_error():
    with pytest.raises(RuntimeError, match="not found"):
        resolve_command(("agent", "acp"), which=lambda name: None)


@pytest.mark.parametrize("shim", ["C:/npm/agent.CMD", "C:/npm/agent.bat"])
def test_an_npm_shim_is_refused_with_a_hint(shim):
    with pytest.raises(RuntimeError, match="executable"):
        resolve_command(("agent", "acp"), which=lambda name: shim)


def executable(folder: Path, name: str) -> Path:
    """A file the OS would run as `name` from `folder` (`name.exe` on Windows)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (f"{name}.exe" if sys.platform == "win32" else name)
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_a_program_in_the_current_folder_is_never_the_agent(tmp_path, monkeypatch):  # F3
    # Windows' own lookup searches the current folder first: a checkout carrying
    # `agent.exe` would run on the host, unsandboxed
    executable(tmp_path / "checkout", "agent")
    monkeypatch.chdir(tmp_path / "checkout")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    path = os.pathsep.join(["", ".", str(tmp_path / "empty")])
    with pytest.raises(RuntimeError, match="not found"):
        resolve_command(("agent", "acp"), path=path)


def test_the_agent_is_found_on_path_as_an_absolute_path(tmp_path, monkeypatch):  # F3
    installed = executable(tmp_path / "bin", "agent")
    monkeypatch.chdir(tmp_path)
    command = resolve_command(
        ("agent", "acp"), path=os.pathsep.join([".", "bin", str(installed.parent)])
    )
    assert Path(command[0]).is_absolute()
    assert Path(command[0]).resolve() == installed.resolve() and command[1:] == ["acp"]


def test_a_program_inside_the_step_tree_is_never_the_agent(tmp_path):  # F3
    tree = tmp_path / "tree"
    executable(tree / "tools", "agent")
    with pytest.raises(RuntimeError, match="inside the step's tree"):
        resolve_command(("agent", "acp"), path=str(tree / "tools"), tree=tree)


def test_a_relative_agent_path_is_refused(tmp_path):  # F3
    with pytest.raises(RuntimeError, match="full path"):
        resolve_command((str(Path("bin") / "agent"), "acp"), path=str(tmp_path))


def test_the_agent_s_own_server_gets_a_free_port_called_directly(world, monkeypatch):
    profile = fake_profile(own_server=("--port", "127.0.0.1"), inherit=("NO_PROXY",))
    monkeypatch.setenv("NO_PROXY", "corp.example")
    run(world.runner([[{"say": "VALID"}]], profile=profile))
    (init,) = world.events("initialize")
    argv = init["argv"]
    port = int(argv[argv.index("--port") + 1])
    assert 0 < port < 65536
    # only that one host:port skips a proxy: a gateway elsewhere on loopback does not
    assert init["env"]["NO_PROXY"] == f"corp.example,127.0.0.1:{port}"
    assert init["env"]["no_proxy"] == init["env"]["NO_PROXY"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows environment names are case-blind")
def test_a_lower_case_no_proxy_is_merged_too(world, monkeypatch):  # DF-449
    profile = fake_profile(own_server=("--port", "127.0.0.1"), inherit=("NO_PROXY", "no_proxy"))
    monkeypatch.setenv("NO_PROXY", "corp.example")
    monkeypatch.setenv("no_proxy", "lab.example,corp.example")
    run(world.runner([[{"say": "VALID"}]], profile=profile))
    (init,) = world.events("initialize")
    port = init["argv"][init["argv"].index("--port") + 1]
    expected = f"corp.example,lab.example,127.0.0.1:{port}"
    assert init["env"]["NO_PROXY"] == init["env"]["no_proxy"] == expected


@pytest.mark.skipif(sys.platform == "win32", reason="wrapping needs macOS, Linux or WSL2")
def test_under_a_wrap_only_the_own_server_bypasses_the_sandbox_proxy(world):
    runner = world.runner([], profile=fake_profile(own_server=("--port", "127.0.0.1")))
    runner.wrap = ["srt", "--settings", "s.json", "--", "env", "NO_PROXY="]
    argv, env = runner._launch()
    at = argv.index("srt")
    assert argv[at : at + 6] == ["srt", "--settings", "s.json", "--", "env", "NO_PROXY="]
    port = argv[argv.index("--port") + 1]
    direct = f"127.0.0.1:{port}"
    assert argv[at + 6 : at + 9] == ["env", f"NO_PROXY={direct}", f"no_proxy={direct}"]
    assert argv[at + 9] == sys.executable
    assert env["NO_PROXY"] == direct


@pytest.mark.skipif(sys.platform != "win32", reason="the refusal is for native Windows")
def test_wrapping_in_a_sandbox_on_windows_is_refused(world):
    runner = world.runner([[{"say": "VALID"}]])
    runner.wrap = ["srt", "--settings", "x", "--"]
    with pytest.raises(RuntimeError, match="WSL2"):
        run(runner)


# ── no fail-open defaults (DF-445) ──────────────────────────────────────────────


@pytest.mark.parametrize("missing", ["rules_for", "wrap_for"])
def test_a_backend_without_rules_or_a_sandbox_wrap_is_refused(missing):
    given = {"rules_for": lambda step: None, "wrap_for": lambda step: None}
    del given[missing]
    settings = AcpSettings(gateway_url=GATEWAY, api_key="k", home_root=Path("h"))
    with pytest.raises(ValueError, match=missing):
        AcpBackend(
            fake_profile(),
            settings,
            identity_for=lambda s: IDENTITY,
            prices=lambda m: PRICE,
            **given,
        )


def test_tests_may_run_unsandboxed_only_by_saying_so():
    settings = AcpSettings(gateway_url=GATEWAY, api_key="k", home_root=Path("h"))
    backend = AcpBackend(
        fake_profile(),
        settings,
        identity_for=lambda s: IDENTITY,
        prices=lambda m: PRICE,
        unsandboxed=True,
    )
    assert backend.rules_for is None and backend.wrap_for is None
