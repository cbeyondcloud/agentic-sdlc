"""A pipeline step on each agent-CLI backend, through `agentic run` (M2-60, decision 89).

Each backend runs against its scripted stand-in - no model and no vendor CLI is ever
asked (owner decision 73):

  * claude-code - the Claude Code binary bundled in the pinned SDK wheel, against the
                  TokenLean stand-in, every other host refused by a proxy;
  * codex       - `fake_codex.py` (speaks `codex exec --json`), wrapped by a stand-in srt
                  through `StepWiring.process_wrap_for`: codex never runs unsandboxed;
  * opencode    - the OpenCode profile against `acp_fake_agent.py`, a scripted ACP agent.

Per backend: the step completes with a valid handoff; a kill at a turn ends the run
`killed` at once; a pause lets the step finish and holds the run before the next step.
"""

import functools
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pytest
from _cli_support import answer, write

from agentic.killswitch import RUN, Verdict
from agentic.sandbox.readiness import Readiness
from agentic_cli import agent_backends, runtime
from agentic_cli import wiring as wiring_module
from agentic_cli.exit_codes import ExitCode

BACKENDS_TESTS = Path(__file__).resolve().parents[3] / "core" / "tests" / "backends"
if str(BACKENDS_TESTS) not in sys.path:  # the core suite's scripted stand-ins
    sys.path.insert(0, str(BACKENDS_TESTS))

HERE = Path(__file__).resolve().parent
FAKE_SRT = HERE / "_fake_srt.py"
FAKE_CODEX = HERE / "_fake_codex_cli.py"
FAKE_ACP = BACKENDS_TESTS / "acp_fake_agent.py"
KEY = "tl-" + "agent-cli-key-0003"  # gitleaks:allow (not a real key)
KILL = Verdict("kill", reason="incident 7", by="admin@acme.example")
PAUSE = Verdict("pause", reason="maintenance", by="admin@acme.example")
ALL = "[native, claude-code, codex, opencode]"


def agent_policy(policy_repo: Path, backend: str) -> None:
    """The run-side test policy, with every backend allowed and both agents on `backend`."""
    classes = (policy_repo / "policy" / "classifications.yaml").read_text(encoding="utf-8")
    write(
        policy_repo / "policy" / "classifications.yaml",
        classes.replace("allowed_backends: [native]", f"allowed_backends: {ALL}"),
    )
    types = (policy_repo / "policy" / "agent-types.yaml").read_text(encoding="utf-8")
    write(
        policy_repo / "policy" / "agent-types.yaml",
        types.replace("backends: [native]", f"backends: {ALL}"),
    )
    for name, kind, version in (("ba", "analyst", "1.0.0"), ("architect", "designer", "2.1.0")):
        write(
            policy_repo / "agents" / f"{name}.md",
            f"---\nname: {name}\ntype: {kind}\nversion: {version}\nbackend: {backend}\n"
            f"description: {name}.\n---\nYou are {name}.\n",
        )
    steps = "  - id: requirements\n    agent: ba\n  - id: design\n    agent: architect\n"
    head = "version: 1.0.0\nregime: team\ninputs:\n  request: { from: [text] }\nsteps:\n"
    write(policy_repo / "pipelines" / "two.yaml", f"name: two\n{head}{steps}")


def start(cli, pipeline="two"):
    result = cli("--json", "run", pipeline, "--input", "request=CSV export")
    return result, (result.json().get("run_id") if result.out.strip() else None)


class Switch:
    """A kill switch: each step's first check (the engine's, before the step) says run;
    every later one for a step - its turns, the next step's own check - says `verdict`."""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict, self.seen = verdict, Counter()

    def check(self, scope):
        if scope.agent is None:  # the run as a whole
            return RUN
        self.seen[scope.agent] += 1
        first_step = scope.agent.startswith("ba@") and self.seen[scope.agent] == 1
        return RUN if first_step else self.verdict


# ── one world per backend ─────────────────────────────────────────────────────


@pytest.fixture
def base(workspace, policy_repo, monkeypatch):
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "http://127.0.0.1:9")
    return workspace, policy_repo


def user_config(workspace: Path, settings: dict) -> None:
    home = Path(os.environ["AGENTIC_HOME"])
    write(home / "config.yaml", json.dumps(settings))


@pytest.fixture
def claude_code(base, monkeypatch):
    pytest.importorskip("claude_agent_sdk", reason="the claude-code extra is not installed")
    from anthropic_stub import RefusingProxy, Reply, TokenLeanStub

    workspace, policy_repo = base
    agent_policy(policy_repo, "claude-code")
    stub, proxy = TokenLeanStub(), RefusingProxy()
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", stub.url)
    for name in ("HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.setenv(name, proxy.url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    def script(*answers: str) -> None:
        stub.queue = [Reply(text=a) for a in answers]

    yield script, stub
    stub.close()
    proxy.close()


@pytest.fixture
def codex(base, tmp_path, monkeypatch):
    import agentic.backends.codex as codex_module
    from agentic.sandbox import SrtSandbox

    workspace, policy_repo = base
    agent_policy(policy_repo, "codex")
    # codex runs only under srt: this machine "has" it, and a stand-in srt runs the step
    monkeypatch.setattr(runtime, "machine_readiness", lambda: Readiness("linux", ()))
    fake_srt = functools.partial(SrtSandbox, srt=(sys.executable, str(FAKE_SRT)))
    monkeypatch.setattr(wiring_module, "SrtSandbox", fake_srt)
    monkeypatch.setattr(codex_module, "native_windows", lambda: False)
    monkeypatch.setattr(agent_backends, "native_windows", lambda: False)
    srt_log, plan, log = tmp_path / "srt.jsonl", tmp_path / "plan.json", tmp_path / "codex.jsonl"
    monkeypatch.setenv("FAKE_SRT_LOG", str(srt_log))
    monkeypatch.setenv("FAKE_CODEX_PLAN", str(plan))
    user_config(workspace, {"agent_clis": {"codex": [sys.executable, str(FAKE_CODEX)]}})

    def script(*answers: str) -> None:
        texts = [{"text": json.dumps({"document": a})} for a in answers]
        plan.write_text(json.dumps({"answers": texts, "log": str(log)}), encoding="utf-8")

    def records(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    return script, (lambda: records(srt_log), lambda: records(log))


@pytest.fixture
def opencode(base, tmp_path, monkeypatch):
    pytest.importorskip("acp", reason="the acp extra is not installed")
    workspace, policy_repo = base
    agent_policy(policy_repo, "opencode")
    script_path, log = tmp_path / "acp-script.json", tmp_path / "acp.jsonl"
    command = [sys.executable, str(FAKE_ACP), str(script_path)]
    user_config(workspace, {"agent_clis": {"opencode": command}})

    def script(*answers: str) -> None:
        turns = [[{"say": a}, {"cost": 0.001}] for a in answers]
        script_path.write_text(json.dumps({"log": str(log), "turns": turns}), encoding="utf-8")

    def events(name: str) -> list[dict]:
        if not log.exists():
            return []
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if row["event"] == name]

    return script, events


REQUIREMENTS, DESIGN = answer("requirements"), answer("design")


# ── a step through each backend ───────────────────────────────────────────────


def test_a_claude_code_step_runs_through_tokenlean_with_the_run_identity(claude_code, cli):
    script, stub = claude_code
    script(REQUIREMENTS, DESIGN)
    result, run_id = start(cli)
    assert result.code == ExitCode.OK, result.out + result.err
    calls = stub.model_calls()
    assert calls and all(call["headers"]["x-agentic-run-id"] == run_id for call in calls)
    assert {call["headers"]["x-agentic-agent"] for call in calls} == {
        "ba@1.0.0",
        "architect@2.1.0",
    }
    assert all(call["headers"]["x-agentic-team"] == "payments" for call in calls)


def test_a_codex_step_runs_inside_the_sandbox_with_the_guards_rules(codex, cli):
    script, (srt_runs, codex_starts) = codex
    script(REQUIREMENTS, DESIGN)
    result, run_id = start(cli)
    assert result.code == ExitCode.OK, result.out + result.err
    runs = srt_runs()
    assert len(runs) == 2  # each step's exec went through the sandbox wrap
    hosts = runs[0]["settings"]["network"]["allowedDomains"]
    assert hosts == ["127.0.0.1:9"]  # the gateway alone: nothing else gets out
    denied = runs[0]["settings"]["filesystem"]["denyWrite"]
    assert any(path.endswith("/config.toml") for path in denied)  # its own config (DF-425)
    rules = codex_starts()[0]["rules"]
    assert '"git", "config"' in rules or '"git", "push"' in rules  # the framework floor


def test_an_opencode_step_runs_through_the_acp_backend(opencode, cli):
    script, events = opencode
    script(REQUIREMENTS, DESIGN)
    result, run_id = start(cli)
    assert result.code == ExitCode.OK, result.out + result.err
    # one agent process and session per step (each new process replays the script from
    # its first turn, so the design step answers on its retry)
    assert len(events("new_session")) == 2
    env = events("initialize")[0]["env"]
    assert "agentic" in Path(env["HOME"]).as_posix()  # a home of its own, under run state


def test_preflight_refuses_a_backend_this_machine_cannot_run(base, cli, monkeypatch):
    workspace, policy_repo = base
    agent_policy(policy_repo, "claude-code")
    monkeypatch.setattr(agent_backends, "missing", lambda b, s: f"install {b} (test)")
    result = cli("--json", "run", "two", "--dry-run", "--input", "request=x")
    assert result.code == ExitCode.INVALID
    problems = result.json()["preflight"]["problems"]
    assert any("install claude-code (test)" in p["detail"] for p in problems)
    assert "Traceback" not in result.err


def test_codex_without_a_sandbox_is_refused_before_any_spend(base, cli, monkeypatch):
    workspace, policy_repo = base
    agent_policy(policy_repo, "codex")
    monkeypatch.setattr(agent_backends, "missing", lambda backend, settings: None)
    result = cli("--json", "run", "two", "--dry-run", "--input", "request=x")
    assert result.code == ExitCode.INVALID
    problems = result.json()["preflight"]["problems"]
    assert any("srt" in p["detail"] and p["severity"] == "error" for p in problems)


# ── the kill switch at a turn, per backend ────────────────────────────────────


def _status(cli, run_id):
    shown = cli("--json", "status", run_id)
    assert shown.code == ExitCode.OK, shown.err
    return shown.json()


@pytest.mark.parametrize("backend", ["claude_code", "codex", "opencode"])
def test_a_kill_at_a_turn_stops_the_run_killed(backend, request, cli, monkeypatch):
    script = request.getfixturevalue(backend)[0]
    script(REQUIREMENTS, DESIGN)
    switch = Switch(KILL)
    monkeypatch.setattr(runtime.Runtime, "killswitch", lambda self: switch)
    # the engine's watchdog would poll only after an hour: the turn hook stops the step
    monkeypatch.setattr(runtime.killswitch_port, "POLL_S", 3600.0)
    result, run_id = start(cli)
    assert result.code != ExitCode.OK, result.out
    shown = _status(cli, run_id)
    assert shown["status"] == "killed"
    assert "incident 7" in (shown.get("end_reason") or "")
    # the turn hook stopped the requirements step itself: it never completed, and the
    # next step's own check (which would kill the run too) was never asked (review F2)
    history = [(row["step"], row["outcome"]) for row in shown["history"]]
    assert history == [("requirements", "stopped")]
    assert shown["history"][0]["detail"].startswith("killed")
    assert switch.seen["ba@1.0.0"] >= 2  # the engine's pre-step check, then a turn
    assert "architect@2.1.0" not in switch.seen


@pytest.mark.parametrize("backend", ["claude_code", "codex", "opencode"])
def test_a_pause_holds_at_the_next_step(backend, request, cli, monkeypatch):
    script = request.getfixturevalue(backend)[0]
    script(REQUIREMENTS, DESIGN)
    monkeypatch.setattr(runtime.Runtime, "killswitch", lambda self: Switch(PAUSE))
    result, run_id = start(cli)
    assert result.code == ExitCode.PAUSED, result.out + result.err
    shown = _status(cli, run_id)
    assert shown["pending"]["id"] == "hold"
    history = [(row["step"], row["outcome"]) for row in shown["history"]]
    assert history == [("requirements", "completed")]  # the step finished; design waits
