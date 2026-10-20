"""Shared helpers for the CLI tests: a small org policy repo, a target git repository, a
fake TokenLean gateway (no real provider is ever called) and a way to run `agentic`."""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx2

from agentic.handoffs.schema import BUILTIN_CATALOG, render_template
from agentic.yamlio import dump_yaml, load_yaml, split_front_matter
from agentic_cli.main import main

GIT = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]
USER = "dev@example.invalid"

POLICY_FILES = {
    "tiers.yaml": "tiers:\n  balanced: { model: acme/model-b }\n  fast: { model: acme/model-f }\n",
    "classifications.yaml": (
        "default_classification: internal\n"
        "classifications:\n"
        "  internal: { allowed_tiers: [balanced, fast], allowed_backends: [native] }\n"
        "  restricted: { allowed_tiers: [fast], allowed_backends: [native] }\n"
    ),
    "tools.yaml": (
        "catalog: [read_files, search, edit_files, run_shell, codeintel, spawn_agent,"
        " activate_skill]\nforbidden: []\n"
    ),
    "org.yaml": "allow_repo_pipelines: true\nsandbox: { required_for: [] }\n",
    "agent-types.yaml": """types:
  analyst:
    description: Writes requirements.
    tier: fast
    backends: [native]
    tools: []
    produces: requirements
    limits:
      max: { budget_usd: 2, max_turns: 10, max_minutes: 10 }
      children_max: { max_total: 0, max_concurrent: 0, max_depth: 0,
                      per_child: { budget_usd: 0, max_turns: 0, max_minutes: 0 } }
  designer:
    description: Writes a design.
    tier: balanced
    backends: [native]
    tools: []
    consumes: [requirements]
    produces: design
    limits:
      max: { budget_usd: 2, max_turns: 10, max_minutes: 10 }
      children_max: { max_total: 0, max_concurrent: 0, max_depth: 0,
                      per_child: { budget_usd: 0, max_turns: 0, max_minutes: 0 } }
  general:
    description: Any other agent.
    tier: fast
    backends: [native]
    tools: [read_files, search, edit_files]
    consumes: [requirements, design, dev-report, review, test-report]
    limits:
      max: { budget_usd: 1, max_turns: 10, max_minutes: 10 }
      children_max: { max_total: 0, max_concurrent: 0, max_depth: 0,
                      per_child: { budget_usd: 0, max_turns: 0, max_minutes: 0 } }
""",
    "teams/payments.yaml": "allowed_tiers: [balanced, fast]\n",
}

AGENTS = {
    "ba": "name: ba\ntype: analyst\nversion: 1.0.0\ndescription: Business analyst.\n",
    "architect": "name: architect\ntype: designer\nversion: 2.1.0\ndescription: Designs.\n",
}

PIPELINE = """name: deliver
version: 1.0.0
regime: team
inputs:
  request: { from: [text] }
steps:
  - id: requirements
    agent: ba
  - gate: requirements-approval
    approver_role: team-lead
  - id: design
    agent: architect
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def make_policy_repo(root: Path) -> Path:
    for name, text in POLICY_FILES.items():
        write(root / "policy" / name, text)
    for name, text in AGENTS.items():
        write(root / "agents" / f"{name}.md", f"---\n{text}---\nYou are {name}.\n")
    write(root / "pipelines" / "deliver.yaml", PIPELINE)
    return root


def make_repo(root: Path) -> Path:
    write(root / "src" / "app.py", "x = 1\n")
    write(root / ".gitignore", ".agentic/local/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", USER)
    git(root, "config", "user.name", "Dev")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def answer(kind: str, **fields) -> str:
    """A valid answer for `kind`: its template with some fields replaced."""
    header, body = split_front_matter(render_template(BUILTIN_CATALOG.schema(kind)))
    values, _ = load_yaml(header)
    values.update(fields)
    return f"---\n{dump_yaml(values)}---\n{body}\n"


@dataclass
class FakeGateway:
    """TokenLean's OpenAI-compatible route, scripted: each request gets the next answer
    for the agent named in its identity header, with a reported cost."""

    answers: dict[str, list[str | dict]]
    cost_usd: str = "0.0125"
    requests: list[dict] = field(default_factory=list)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        agent = request.headers.get("x-agentic-agent", "?").split("@")[0]
        self.requests.append({"agent": agent, "path": request.url.path})
        queue = self.answers.get(agent) or ["no answer scripted"]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, dict):  # {"tool": name, "args": {...}}: the model calls a tool
            call = {
                "id": f"call-{len(self.requests)}",
                "type": "function",
                "function": {"name": reply["tool"], "arguments": json.dumps(reply["args"])},
            }
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": reply}, "stop"
        body = {
            "id": f"chatcmpl-{len(self.requests)}",
            "object": "chat.completion",
            "created": 0,
            "model": "model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        headers = {"x-tokenlean-cost-usd": self.cost_usd, "x-tokenlean-request-id": "req"}
        return httpx2.Response(200, json=body, headers=headers)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handler)


@dataclass
class Result:
    code: int
    out: str
    err: str

    def json(self):
        return json.loads(self.out)


def runner(capsys) -> Callable[..., Result]:
    def run(*args: str) -> Result:
        code = main(list(args))
        captured = capsys.readouterr()
        return Result(int(code), captured.out, captured.err)

    return run
