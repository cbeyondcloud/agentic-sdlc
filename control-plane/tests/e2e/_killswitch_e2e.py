"""The kill-switch propagation E2E's harness (M2-54): the real control plane under uvicorn,
an org policy it syncs, a target repository with a slow command, a scripted model, a thread
that flips a switch while a run is mid-step, and the timings of each run.

Timing rules (DF-688 lesson): every wait is a poll with a deadline, never a fixed sleep
that an assertion depends on; process deadlines are generous, the asserted bound is the
measured one. A unique module name, so it never collides with another folder's helpers."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx2
import uvicorn
from policy_repos import commit, make_policy_repo, write

from agentic_cli.control_plane import ControlPlaneClient

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The row's bound: a flipped kill switch stops a running pipeline within this (M2-54).
KILL_BOUND_S = 30.0
#: Consecutive runs per scope that must all pass ("5/5").
RUNS = 5
#: How long a run may take to reach its slow turn (startup on a loaded CI host).
START_DEADLINE_S = 120.0
#: How long the step's processes may take to be gone (far above the asserted bound).
GONE_DEADLINE_S = 60.0
#: When a stop never arrives, the slow turn is released this long after the flip, so a
#: regression fails the test instead of hanging it.
SAFETY_RELEASE_S = 3 * KILL_BOUND_S
#: The variable naming the folder where the slow command writes its pids and waits for
#: its release file (not secret-looking, so the sandbox's environment filter keeps it).
SIGNAL_ENV = "KILLSWITCH_E2E_DIR"
TEAM, PIPELINE, AGENT = "payments", "e2e", "worker"
MODEL_KEY = "tl-" + "e2e-scripted-model-0054"  # gitleaks:allow (not a real key)


def _load_process_support():
    """`core/tests/_process_support.py`, loaded by path: one `alive` for every test."""
    name = "_process_support"
    if name not in sys.modules:
        path = REPO_ROOT / "core" / "tests" / "_process_support.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


alive = _load_process_support().alive


def wait_for[T](probe: Callable[[], T], within_s: float, every_s: float = 0.05) -> T | None:
    """`probe()` once it is truthy, polled until the deadline; None when it never is."""
    deadline = time.monotonic() + within_s
    while True:
        found = probe()
        if found:
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(every_s)


# ── the control plane ───────────────────────────────────────────────────────


class StateLog:
    """ASGI wrapper: the monotonic time each `GET /v1/killswitch/state` answer started -
    when a CLI learned the switch state."""

    PATH = "/v1/killswitch/state"

    def __init__(self, app) -> None:
        self.app = app
        self.served: list[float] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != self.PATH:
            return await self.app(scope, receive, send)

        async def sending(message):
            if message["type"] == "http.response.start":
                self.served.append(time.monotonic())
            await send(message)

        return await self.app(scope, receive, sending)

    def first_after(self, moment: float) -> float | None:
        return next((t for t in self.served if t > moment), None)


class LiveServer:
    """The app under uvicorn on 127.0.0.1 in a thread with its own loop and engine."""

    def __init__(self, app) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        started = wait_for(lambda: self.server.started or not self.thread.is_alive(), 60)
        if not started or not self.server.started:
            raise RuntimeError("the control plane did not start")
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        """Stop serving: afterwards a connection to the port is refused. Idempotent."""
        self.server.should_exit = True
        self.thread.join(timeout=60)


@dataclass
class Plane:
    base: str
    admin_email: str
    admin_token: str
    dev_email: str
    dev_token: str
    log: StateLog
    server: LiveServer
    version: str = ""

    def admin(self) -> ControlPlaneClient:
        """A fresh client per caller thread (the CLI's own client, over HTTP)."""
        return ControlPlaneClient(self.base, self.admin_token)


# ── the org policy and the target repository ────────────────────────────────

# the pipeline's bytes as committed: CRLF line endings and a non-ASCII character, so a
# line-ending or encoding difference between the control plane's hash and the CLI's
# (D33, DF-518) would refuse the run
PIPELINE_BYTES = (
    "# Kill-switch propagation E2E — one slow step\r\n"
    f"name: {PIPELINE}\r\nversion: 1.0.0\r\nregime: team\r\n"
    f"steps:\r\n  - id: work\r\n    agent: {AGENT}\r\n"
).encode()

POLICY_FILES = {
    "policy/tiers.yaml": (
        "tiers:\n  deep-reasoning: { model: acme/model-d }\n  balanced: { model: acme/model-b }\n"
        "  fast: { model: acme/model-f }\n  local: { model: local/model-l }\n"
    ),
    "policy/org.yaml": (
        "allowed_repo_patterns: []\nallow_repo_pipelines: false\nsandbox: { required_for: [] }\n"
    ),
    "policy/limits.yaml": "concurrency: { per_user: 100, per_team: 100 }\n",
    "policy/agent-types.yaml": f"""types:
  {AGENT}:
    description: Runs the command it is told to.
    tier: fast
    backends: [native]
    tools: [run_shell]
    shell: [python]
    limits:
      max: {{ budget_usd: 5, max_turns: 20, max_minutes: 30 }}
      children_max: {{ max_total: 0, max_concurrent: 0, max_depth: 0,
                      per_child: {{ budget_usd: 0, max_turns: 0, max_minutes: 0 }} }}
""",
    f"agents/{AGENT}.md": (
        f"---\nname: {AGENT}\ntype: {AGENT}\nversion: 1.0.0\n"
        "description: Runs a slow command.\n---\nRun the command you are given.\n"
    ),
}


def make_e2e_policy_repo(path: Path) -> Path:
    """The standalone template's policy (as the control plane's sync tests use it) with
    team `payments`, one agent type and agent, and the CRLF pipeline."""
    repo = make_policy_repo(path, teams=(TEAM,))
    for rel, text in POLICY_FILES.items():
        write(repo, rel, text)
    target = repo / "pipelines" / f"{PIPELINE}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PIPELINE_BYTES)
    commit(repo, "kill-switch e2e policy")
    return repo


# The slow command: a child that starts a grandchild, writes both pids, and waits (as does
# the grandchild) for a release file - with a lifetime, so nothing outlives a failed test.
SPAWNER = """\
import os
import subprocess
import sys
import time
from pathlib import Path

LIFETIME_S = 240
where = Path(os.environ["KILLSWITCH_E2E_DIR"])


def wait():
    deadline = time.monotonic() + LIFETIME_S
    while time.monotonic() < deadline and not (where / "release").exists():
        time.sleep(0.05)


if sys.argv[1:] == ["grandchild"]:
    wait()
    sys.exit(0)
grandchild = subprocess.Popen([sys.executable, os.path.abspath(__file__), "grandchild"])
staged = where / "pids.tmp"
staged.write_text(f"{os.getpid()} {grandchild.pid}", encoding="utf-8")
os.replace(staged, where / "pids")
wait()
grandchild.wait()
print("released")
"""
SLOW_COMMAND = "python spawner.py"
GIT = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev", "-c", "commit.gpgsign=false"]


def git(where: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(where), *GIT, *args], capture_output=True, check=True)


def make_target_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "spawner.py").write_bytes(SPAWNER.encode())
    (path / ".gitignore").write_bytes(b".agentic/local/\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    git(path, "remote", "add", "origin", "https://github.com/acme/app.git")
    return path


def read_pids(where: Path) -> list[int] | None:
    try:
        return [int(p) for p in (where / "pids").read_text(encoding="utf-8").split()]
    except (OSError, ValueError):
        return None


def release(where: Path) -> None:
    (where / "release").write_bytes(b"")


# ── the scripted model ──────────────────────────────────────────────────────


@dataclass
class ScriptedModel:
    """The gateway's OpenAI-compatible route, scripted: each request gets the next answer
    (a `{"tool": name, "args": {...}}` is a tool call), with a reported cost. Records the
    monotonic time and body of every request."""

    answers: list[str | dict]
    calls: list[float] = field(default_factory=list)
    bodies: list[dict] = field(default_factory=list)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(time.monotonic())
        self.bodies.append(json.loads(request.content))
        reply = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        n = len(self.calls)
        if isinstance(reply, dict):
            call = {
                "id": f"call-{n}",
                "type": "function",
                "function": {"name": reply["tool"], "arguments": json.dumps(reply["args"])},
            }
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": reply}, "stop"
        body = {
            "id": f"chatcmpl-{n}",
            "object": "chat.completion",
            "created": 0,
            "model": "model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        headers = {"x-tokenlean-cost-usd": "0.001", "x-tokenlean-request-id": f"req-{n}"}
        return httpx2.Response(200, json=body, headers=headers)

    def after(self, moment: float) -> int:
        """Model calls started after `moment`."""
        return sum(1 for t in self.calls if t > moment)


def slow_turn_script() -> list[str | dict]:
    """Turn 1 runs the slow command; turn 2 answers."""
    return [{"tool": "run_shell", "args": {"command": SLOW_COMMAND}}, "Done: the command ran."]


# ── one run with something done to it mid-step ──────────────────────────────


@dataclass
class Timing:
    """One run: seconds from the flip (or outage) to each milestone."""

    scenario: str
    run: int
    to_aware_s: float | None  # the control plane served the CLI a state poll
    to_stopped_s: float  # the CLI's command returned with the run killed or held
    to_gone_s: float | None  # the step's child and grandchild were gone
    calls_after: int  # model calls started after the flip

    def line(self) -> str:
        def s(value: float | None) -> str:
            return "never" if value is None else f"{value:5.1f} s"

        return (
            f"{self.scenario:<10} run {self.run}: aware {s(self.to_aware_s)}, "
            f"stopped {s(self.to_stopped_s)}, processes gone {s(self.to_gone_s)}, "
            f"model calls after {self.calls_after}"
        )


#: Every run's timing in this session, for the terminal summary (conftest).
TIMINGS: list[Timing] = []


def report(timings: list[Timing]) -> str:
    return "\n".join(t.line() for t in timings)


class MidStep(threading.Thread):
    """Waits until the run is in its slow turn (the pid file), then runs `act` and records
    when it started; a stop that never comes releases the turn after `SAFETY_RELEASE_S`."""

    def __init__(self, where: Path, act: Callable[[MidStep], None]) -> None:
        super().__init__(daemon=True)
        self.where, self.act = where, act
        self.done = threading.Event()  # set by the test when the CLI command returned
        self.pids: list[int] = []
        self.acted_at: float | None = None
        self.error: str | None = None
        self.notes: dict[str, object] = {}

    def run(self) -> None:
        try:
            pids = wait_for(lambda: read_pids(self.where) or self.done.is_set(), START_DEADLINE_S)
            if not isinstance(pids, list):
                self.error = "the run never reached its slow turn"
                return
            self.pids = pids
            self.acted_at = time.monotonic()
            self.act(self)
            if not wait_for(self.done.is_set, SAFETY_RELEASE_S):
                self.error = f"the run did not stop within {SAFETY_RELEASE_S:.0f} s"
        except BaseException as error:  # noqa: BLE001 - reported by the test
            self.error = f"{type(error).__name__}: {error}"
        finally:
            if self.error:
                release(self.where)

    def finish(self) -> None:
        self.done.set()
        self.join(timeout=SAFETY_RELEASE_S + 30)


def gone_after(pids: list[int], moment: float) -> float | None:
    """Seconds from `moment` until none of `pids` is alive (None: not within the deadline)."""
    if not pids:
        return None
    if wait_for(lambda: not any(alive(pid) for pid in pids), GONE_DEADLINE_S) is None:
        return None
    return time.monotonic() - moment
