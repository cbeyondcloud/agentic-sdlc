"""Plumbing for the M1 acceptance scenarios (spec §1.3, M1-68..M1-70).

Each scenario drives the real `agentic` CLI, in this process, on a temporary git
repository, with:

  * **models** - the eval cassette server (`agentic.eval.replay`) where TokenLean would
    be: the CLI's gateway URL is the scenario's cassette base URL, so the native backend,
    metering, the handoff contract and the HTTP transport all run for real. Replay (the
    default) answers from `cassettes/<scenario>/acceptance/1.json` and fails on a missing,
    extra or unused call. Record (`uv run python scripts/record_acceptance.py`) forwards
    each call to `ScriptedUpstream` - hand-written answers per pipeline step, no model, no
    network - and writes the cassette only when every call succeeded;
  * **GitHub** - `FakeGitHub` (cli/tests/fake_github.py) and a local bare remote, wired
    as in the M1-58 contract tests: pushes really land in the bare repo and the pull
    request is really opened through the hosting adapter;
  * **the person** - the CLI's gate verbs, with a scripted `$EDITOR` for `agentic edit`;
  * **the machine** - real git, real worktrees, real shell commands (pytest in the run's
    tree) and the real sandbox decision; `agentic init`'s code index build is faked.

Nothing here reads the developer's real home, keyring or network settings.
"""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from cli_helpers import GIT
from fake_github import FakeGitHub

from agentic.eval.replay import CassetteServer, UpstreamReply, UpstreamRequest
from agentic.handoffs.schema import BUILTIN_CATALOG, render_template
from agentic.yamlio import dump_yaml, load_yaml, split_front_matter
from agentic_cli.main import main

HERE = Path(__file__).resolve().parent
SAMPLE_REPO = HERE / "sample-repo"
CASSETTES = HERE / "cassettes"
#: `scripts/record_acceptance.py` sets this: answer from the scripted upstream and record
RECORD = os.environ.get("AGENTIC_ACCEPTANCE_RECORD", "").strip() == "1"
RERECORD = "uv run python scripts/record_acceptance.py"
ROOT = HERE.parents[2]
#: What the scenarios' prompts are made of: a change here makes a cassette stale (DF-316).
PROMPT_SOURCES = (
    ROOT / "library" / "agents",
    ROOT / "library" / "pipelines",
    ROOT / "templates" / "standalone" / "policy",
    ROOT / "core" / "src" / "agentic" / "handoffs" / "templates",
)
FINGERPRINT = "prompts.sha256"  # beside each scenario's cassette


def prompt_fingerprint(sources=PROMPT_SOURCES, root: Path = ROOT) -> str:
    """sha256 over every file of `sources` (path, then bytes with CRLF as LF), so a
    Windows and a Linux checkout agree."""
    digest = hashlib.sha256()
    for folder in sources:
        files = sorted(p for p in Path(folder).rglob("*") if p.is_file())
        for path in (p for p in files if "__pycache__" not in p.parts):
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return "sha256:" + digest.hexdigest()


def prompt_drift(recorded: str | None, current: str) -> list[str]:
    """A cassette recorded from other prompts: a warning, or a problem with
    AGENTIC_ACCEPTANCE_STRICT=1, so the drift shows before the live run does (DF-316).
    (The cassette server's own `stale` count cannot tell: the requests carry per-run
    values, run ids and temporary paths, so their hashes differ on every run.)"""
    if recorded == current:
        return []
    message = (
        "the cassette was recorded from other agent instructions, pipelines, policy "
        f"template or handoff templates: re-record with `{RERECORD}`"
    )
    if os.environ.get("AGENTIC_ACCEPTANCE_STRICT", "").strip() == "1":
        return [message]
    warnings.warn(message, UserWarning, stacklevel=2)
    return []


HOST = "github.example.test"
USER = "dev@example.invalid"
KEY = "tl-" + "fake-gateway-key-0068"  # gitleaks:allow (not a real key)
TOKEN = "ghp_" + "Acceptance" + "Fake0123456789"  # gitleaks:allow - built from pieces
COST_USD = "0.0125"  # what each recorded call reports in x-tokenlean-cost-usd
MODELS = {  # the person's choice for each tier of the standalone template
    "change-me/deep-reasoning-model": "acme/deep-model",
    "change-me/balanced-model": "acme/balanced-model",
    "change-me/fast-model": "acme/fast-model",
    "local/change-me": "local/acme-small",
}
CI_MARKERS = ("CI", "GITHUB_ACTIONS", "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE")
CLEARED = (
    "AGENTIC_POLICY_REPO",
    "AGENTIC_FRAMEWORK_ROOT",
    "AGENTIC_TEAM",
    "AGENTIC_USER",
    "AGENTIC_CONTROL_PLANE_URL",
    "VISUAL",
)

EDITOR_SCRIPT = """\
import os, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    text = handle.read()
old, new = os.environ["EDIT_FROM"], os.environ["EDIT_TO"]
if old not in text:
    sys.exit("the text to edit is not in " + path)
with open(path, "w", encoding="utf-8", newline="\\n") as handle:
    handle.write(text.replace(old, new))
"""


# ── scripted answers ─────────────────────────────────────────────────────────


def tool(name: str, **args) -> dict:
    """A scripted model turn that calls one tool."""
    return {"tool": name, "args": args}


def handoff(kind: str, sections: dict[str, str] | None = None, **fields) -> str:
    """A valid answer of `kind`: its template's front matter with `fields` replaced, and
    the guidance of each named section replaced by the text given for it."""
    header, body = split_front_matter(render_template(BUILTIN_CATALOG.schema(kind)))
    values, _ = load_yaml(header)
    values.update(fields)
    lines = body.split("\n")
    for heading, text in (sections or {}).items():
        at = lines.index(f"## {heading}")
        lines[at + 1] = text.strip()
    return f"---\n{dump_yaml(values)}---\n" + "\n".join(lines) + "\n"


class ScriptedUpstream:
    """Stands where TokenLean would while recording: each call gets the next scripted
    turn of the pipeline step named in its `X-Agentic-Step` header (no model is called).
    A step asked for more turns than scripted answers 500, so nothing is recorded."""

    def __init__(self, script: dict[str, list[str | dict]]) -> None:
        self.queues = {step: list(turns) for step, turns in script.items()}
        self.calls: dict[str, int] = defaultdict(int)
        self.requests: list[dict] = []

    def leftovers(self) -> dict[str, int]:
        return {step: len(q) for step, q in self.queues.items() if q}

    def __call__(self, request: UpstreamRequest) -> UpstreamReply:
        step = request.headers.get("x-agentic-step", "?")
        body = json.loads(request.body)
        self.requests.append({"step": step, "body": body})
        self.calls[step] += 1
        queue = self.queues.get(step) or []
        if not queue:
            error = {"error": {"message": f"no scripted turn {self.calls[step]} for {step}"}}
            return UpstreamReply(500, json.dumps(error).encode("utf-8"), "application/json")
        turn = queue.pop(0)
        if isinstance(turn, dict):
            call = {
                "id": f"call_{step}_{self.calls[step]}",
                "type": "function",
                "function": {"name": turn["tool"], "arguments": json.dumps(turn["args"])},
            }
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": turn}, "stop"
        reply = {
            "id": f"chatcmpl-{step}-{self.calls[step]}",
            "object": "chat.completion",
            "created": 1767225600,
            "model": body.get("model", "acceptance"),
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
        }
        headers = {"x-tokenlean-cost-usd": COST_USD, "x-tokenlean-request-id": "acceptance"}
        return UpstreamReply(200, json.dumps(reply).encode("utf-8"), "application/json", headers)


# ── the CLI and git ──────────────────────────────────────────────────────────


@dataclass
class Result:
    code: int
    out: str
    err: str

    def json(self):
        return json.loads(self.out)


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return done.stdout.strip()


def copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(
        source, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
    )


@dataclass
class Scenario:
    """One acceptance scenario's world: a home, a remote, GitHub, the models, the CLI."""

    tmp: Path
    monkeypatch: object
    capsys: object
    name: str = ""
    github: FakeGitHub | None = None
    server: CassetteServer | None = None
    upstream: ScriptedUpstream | None = None
    log: list[str] = field(default_factory=list)  # every command run, for failure messages

    @property
    def home(self) -> Path:
        return self.tmp / "home"

    @property
    def agentic_home(self) -> Path:
        return self.home / ".agentic"

    @property
    def standalone(self) -> Path:
        """The person's standalone policy repo (`agentic init --standalone`, M1-62)."""
        return self.agentic_home / "policy" / "standalone"

    @property
    def bare(self) -> Path:
        return self.tmp / "remote.git"

    # set-up

    def start(self) -> "Scenario":
        mp = self.monkeypatch
        self.home.mkdir(parents=True)
        mp.setenv("HOME", str(self.home))
        mp.setenv("USERPROFILE", str(self.home))
        mp.setenv("AGENTIC_HOME", str(self.agentic_home))
        for name in (*CI_MARKERS, *CLEARED):
            mp.delenv(name, raising=False)
        mp.setenv("AGENTIC_TRACES", "off")  # one global tracer provider per test process
        mp.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
        mp.setenv("AGENTIC_GITHUB_TOKEN", TOKEN)
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")  # no machine-wide autocrlf or hooks
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        self.github = FakeGitHub(TOKEN, self.bare).start()
        config = f"git_hosts:\n  {HOST}:\n    kind: github\n    api_url: {self.github.url}\n"
        self.agentic_home.mkdir()
        (self.agentic_home / "config.yaml").write_text(config, encoding="utf-8")
        self._fake_index_builder()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.close()
        if self.github is not None:
            self.github.stop()

    def _fake_index_builder(self) -> None:
        """`agentic init` builds the code index with the codeintel extra; that build is
        the slowest part of init and is not what these scenarios test (stubbed)."""
        from agentic_cli import dev_init

        def deps():
            made = dev_init.InitDeps()
            made.index_builder = lambda repo, data_dir: "built (acceptance stub)"
            made.confirm = lambda text: False
            return made

        self.monkeypatch.setattr(dev_init, "default_deps", deps)

    def existing_repo(self, name: str = "invoices") -> Path:
        """The sample existing repository, with a little history, pushed to origin."""
        repo = self.tmp / "work" / name
        copy_tree(SAMPLE_REPO, repo)
        self._identity(repo, init=True)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "invoices: totals and formatting")
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 0.3.0\n- format_cents\n", "utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "docs: changelog")
        self.connect_origin(repo)
        git(repo, "push", "-q", "origin", "main")
        return repo

    def empty_repo(self, name: str = "greeter") -> Path:
        """`git init` of a brand-new repository: no files, no commits, an origin."""
        repo = self.tmp / "work" / name
        repo.mkdir(parents=True)
        self._identity(repo, init=True)
        self.connect_origin(repo)
        return repo

    def _identity(self, repo: Path, *, init: bool) -> None:
        if init:
            git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", USER)
        git(repo, "config", "user.name", "Dev")
        git(repo, "config", "core.autocrlf", "false")

    def connect_origin(self, repo: Path) -> None:
        git(repo, "remote", "add", "origin", f"https://{HOST}/acme/{repo.name}.git")
        git(repo, "remote", "set-url", "--push", "origin", str(self.bare))  # pushes go here
        self.monkeypatch.chdir(repo)

    def init_standalone(self, repo: Path, *, new_repo: bool = False) -> dict:
        """Set agentic up as spec §24 says, in standalone mode: the repo owner runs
        `init --repo --standalone` (its pull request opened through the adapter) and the
        PR is merged - or, in a `new_repo` with no commit, follows the two steps init
        gives instead; the developer runs `init --standalone` (not ready: the template's
        models are placeholders), names real models in their policy copy, and runs it
        again (ready). Returns `init --repo`'s report."""
        setup = self.ok("--json", "init", "--repo", "--standalone").json()
        if new_repo:  # no base branch for a pull request yet
            assert setup["pull_request"]["opened"] is False and self.github.pulls == []
            for step in setup["next"].values():  # merge --ff-only, then push -u origin main
                git(repo, *step.split()[1:])
        else:
            assert setup["pull_request"]["opened"] is True, setup
            pull = self.github.pulls[-1]
            assert pull["head"]["ref"] == setup["branch"]
            assert ".agentic/config.yaml" in pull["body"]
            self.merge_setup_pull_request(repo, setup["branch"])

        first = self.ok("--json", "init", "--standalone", code=1).json()
        models = [c for c in first["checks"] if c["name"] == "policy: models"]
        assert models and models[0]["status"] == "missing", first
        self.choose_models(without_sandbox=not sandbox_available())
        self.ok("--json", "init", "--standalone")
        return setup

    def merge_setup_pull_request(self, repo: Path, branch: str) -> None:
        """A person reviews and merges `agentic init --repo`'s pull request (stubbed: a
        local merge of the pushed branch, then main pushed back)."""
        git(repo, "merge", "-q", "--ff-only", branch)
        git(repo, "push", "-q", "origin", "main")

    def choose_models(self, *, without_sandbox: bool) -> None:
        """The person edits their standalone policy copy as its README says: real model
        names for each tier; and, on a machine without an OS sandbox, lets team runs go
        unsandboxed (commands are still checked by the policy guards)."""
        tiers = self.standalone / "policy" / "tiers.yaml"
        text = tiers.read_text(encoding="utf-8")
        for placeholder, model in MODELS.items():
            text = text.replace(placeholder, model)
        tiers.write_text(text, encoding="utf-8")
        if without_sandbox:
            org = self.standalone / "policy" / "org.yaml"
            text = org.read_text(encoding="utf-8")
            text = text.replace("required_for: [team, regulated]", "required_for: []")
            org.write_text(text, encoding="utf-8")

    def editor(self, old: str, new: str) -> None:
        """`agentic edit` opens a scripted editor that replaces `old` with `new`."""
        script = self.tmp / "fake_editor.py"
        script.write_text(EDITOR_SCRIPT, encoding="utf-8")
        self.monkeypatch.setenv("EDITOR", f'"{sys.executable}" "{script}"')
        self.monkeypatch.setenv("EDIT_FROM", old)
        self.monkeypatch.setenv("EDIT_TO", new)

    # models

    def models(self, script: dict[str, list[str | dict]]) -> None:
        """Serve the model calls from this scenario's cassette (or record it)."""
        key = f"{self.name}/acceptance/1"
        if RECORD:
            self.upstream = ScriptedUpstream(script)
            self.server = CassetteServer(CASSETTES, "record", self.upstream).start()
        else:
            cassette = CASSETTES.joinpath(*key.split("/")).with_suffix(".json")
            assert cassette.is_file(), (
                f"no cassette {cassette}: record it with `uv run python scripts/"
                "record_acceptance.py`"
            )
            self.server = CassetteServer(CASSETTES, "replay").start()
        self.server.expect([key])
        self.monkeypatch.setenv("AGENTIC_GATEWAY_URL", self.server.base_url(key))

    def finish_models(self) -> list[str]:
        """The cassette's verdict: written (record) or fully used (replay); [] = faithful.
        A cassette recorded from other prompts warns, or fails when strict (DF-316)."""
        assert self.server is not None
        problems = self.server.finish()
        if self.upstream is not None and self.upstream.leftovers():
            problems.append(f"scripted turns never asked for: {self.upstream.leftovers()}")
        stamp = CASSETTES / self.name / "acceptance" / FINGERPRINT
        current = prompt_fingerprint()
        if RECORD:
            if not problems:  # written with the cassette, only when it was written
                stamp.write_text(current + "\n", encoding="utf-8", newline="\n")
            return problems
        recorded = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None
        return problems + prompt_drift(recorded, current)

    # the CLI

    def cli(self, *args: str) -> Result:
        self.log.append("agentic " + " ".join(args))
        code = main(list(args))
        captured = self.capsys.readouterr()
        return Result(int(code), captured.out, captured.err)

    def ok(self, *args: str, code: int = 0) -> Result:
        """Run a command that must exit with `code`; its JSON output is checked to parse."""
        result = self.cli(*args)
        assert result.code == code, (
            f"`agentic {' '.join(args)}` exited {result.code}, expected {code}\n"
            f"stdout: {result.out[-3000:]}\nstderr: {result.err[-3000:]}"
        )
        return result


# ── what a run left behind ───────────────────────────────────────────────────


def run_folder(repo: Path, run_id: str) -> Path:
    return repo / ".agentic" / "local" / "runs" / run_id


def approvals(repo: Path, run_id: str) -> list[dict]:
    """The run's approval ledger (Zyroflo's approvals.jsonl), entry by entry."""
    text = (run_folder(repo, run_id) / "approvals.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def handoff_text(repo: Path, run_id: str, rel: str) -> str:
    return (run_folder(repo, run_id) / rel).read_text(encoding="utf-8")


def recorded_calls(scenario: str) -> int:
    cassette = CASSETTES / scenario / "acceptance" / "1.json"
    return len(json.loads(cassette.read_bytes())["calls"])


def trailers_by_step(where: Path, branch: str) -> dict[str, list[str]]:
    """The run branch's checkpoint commits, newest first: 'develop #2' -> the handoffs its
    Agentic-Handoff trailers cite (what the step was given)."""
    log = git(where, "log", "--format=%s%x00%(trailers:key=Agentic-Handoff,valueonly)%x01", branch)
    found: dict[str, list[str]] = {}
    for entry in log.split("\x01"):
        subject, _, handoffs = entry.strip().partition("\x00")
        if subject.startswith("agentic: "):
            step = subject.removeprefix("agentic: ").split(" (", 1)[0]
            found[step] = [line.strip() for line in handoffs.splitlines() if line.strip()]
    return found


def shell_results(scenario: Scenario, run_id: str, step: str) -> list[str]:
    """What each command an agent ran in `step` (`<id>-<iteration>`) ended with, from the
    step log (`agentic logs`): 'exit 1 - 1 failed, 4 passed', timings left out."""
    records = scenario.ok("--json", "logs", run_id, "--step", step).json()
    return [
        re.sub(r" in [\d.]+s$", "", r["message"].removeprefix("tool run_shell: "))
        for r in records
        if r["message"].startswith("tool run_shell: ")
    ]


def success(name: str) -> dict:
    return {"name": name, "status": "completed", "conclusion": "success", "html_url": ""}


def assert_published(
    scenario: Scenario, repo: Path, run_id: str, main_head: str, *, title: str
) -> dict:
    """The run ended in a pull request through the hosting adapter: its branch really
    pushed, its body linking the run, its cost and logs; the developer's own checkout
    untouched; no test-run caches committed. Returns the pull request."""
    branch = f"sdlc/{run_id}"
    pull = scenario.github.pulls[-1]
    assert (pull["head"]["ref"], pull["base"]["ref"]) == (branch, "main")
    assert pull["head"]["sha"] == git(scenario.bare, "rev-parse", f"refs/heads/{branch}")
    assert title in pull["title"]
    body = pull["body"]
    assert f"## Agentic run `{run_id}`" in body
    assert f".agentic/local/runs/{run_id}/handoffs/" in body and "sha256:" in body
    assert f"- Cost: ${Decimal(COST_USD) * recorded_calls(scenario.name)}" in body
    assert f"- Logs: `agentic logs {run_id}`" in body
    recorded = json.loads((run_folder(repo, run_id) / "pr.json").read_text(encoding="utf-8"))
    assert recorded["pull_requests"][0]["url"] == pull["html_url"]
    files = git(scenario.bare, "ls-tree", "-r", "--name-only", branch).splitlines()
    assert not [f for f in files if "__pycache__" in f or f.endswith(".pyc")], files
    # the run worked in its own worktree: the checkout is where the person left it
    assert git(repo, "rev-parse", "HEAD") == main_head
    assert git(repo, "status", "--porcelain") == ""
    assert not [p for p in scenario.github.paths() if "merge" in p]  # a person merges
    return pull


def sandbox_available() -> bool:
    """Whether this machine has an OS sandbox ready (the Linux CI container does)."""
    from agentic.sandbox.readiness import readiness

    return readiness().available


def on_windows() -> bool:
    return platform.system() == "Windows"
