"""Record the eval fixture cassettes and expected reports (M1-63 fixture mode).

    uv run python scripts/record_eval_fixtures.py

No model is called. A scripted upstream stands where TokenLean would: it answers each
agent call with the hand-written answer `library/eval/fixtures/script.yaml` names for
the case, and each judge call with the scripted rubric scores (quoting the output's
title as evidence). `agentic eval run --mode record` runs through the real harness -
agent runner, handoff contract, metering, judge, cassette server - so the cassettes hold
exactly the traffic a replay will make. Each agent is then replayed and its comparable
report written to `fixtures/expected/<agent>.json`; review that diff before committing.

Run it after changing a case, a golden answer, a fixture agent, the handoff templates or
the judge prompts (a replay then reports stale cassettes, or fails).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from agentic.eval.replay import UpstreamReply, UpstreamRequest
from agentic.eval.report import comparable
from agentic.yamlio import load_yaml, read_text
from agentic_cli.evaluation import RunOptions, execute_run

REPO = Path(__file__).resolve().parent.parent
LIBRARY = REPO / "library" / "eval"
FIXTURES = LIBRARY / "fixtures"
AGENTS = ["business-analyst", "design", "code-review", "qa", "developer"]
#: Only these cases have cassettes: CI replays them; the rest of the corpus is for live runs.
FIXTURE_TAG = "fixture"


class ScriptedUpstream:
    def __init__(self, script: dict) -> None:
        self.script = script
        self.calls: dict[str, int] = defaultdict(int)

    def __call__(self, request: UpstreamRequest) -> UpstreamReply:
        case_id, _label, _repeat, role = request.key.split("/")
        body = json.loads(request.body)
        index = self.calls[request.key]
        self.calls[request.key] += 1
        if role == "agent":
            answers = self.script["answers"][case_id]
            text = read_text(LIBRARY / answers[min(index, len(answers) - 1)])
            message = {"role": "assistant", "content": text.replace("\r\n", "\n")}
            return self._reply(body, message, "stop")
        prompt = _last_user_text(body)
        output = prompt.split("<output>\n", 1)[1].split("\n</output>", 1)[0]
        title = next((line for line in output.splitlines() if line.startswith("# ")), "")
        wanted = self.script["judge"].get(case_id, {})
        scores = [
            {
                "criterion": criterion,
                "score": wanted.get(criterion, 2),
                "evidence": title if wanted.get(criterion, 2) > 0 else "",
            }
            for criterion in re.findall(r"Criterion '([^']+)'", prompt)
        ]
        call = {
            "id": f"call_{case_id}_{index}",
            "type": "function",
            "function": {
                "name": body["tools"][0]["function"]["name"],
                "arguments": json.dumps({"scores": scores}),
            },
        }
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        return self._reply(body, message, "tool_calls")

    def _reply(self, body: dict, message: dict, finish: str) -> UpstreamReply:
        reply = {
            "id": "chatcmpl-fixture",
            "object": "chat.completion",
            "created": 1767225600,
            "model": body.get("model", "fixture"),
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 400, "total_tokens": 1400},
        }
        headers = {"x-tokenlean-cost-usd": str(self.script["cost_usd"])}
        return UpstreamReply(200, json.dumps(reply).encode("utf-8"), "application/json", headers)


def _without_mode(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != "mode"}


def _last_user_text(body: dict) -> str:
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                return "\n".join(str(part.get("text", "")) for part in content)
            return str(content)
    return ""


def main() -> int:
    script, _ = load_yaml(read_text(FIXTURES / "script.yaml"))
    shutil.rmtree(FIXTURES / "cassettes", ignore_errors=True)
    (FIXTURES / "expected").mkdir(exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="agentic-eval-record-")
    home = Path.cwd()
    os.chdir(scratch)  # the eval ledgers go to the scratch folder, not the repo
    try:
        return _record(script, scratch)
    finally:
        os.chdir(home)
        shutil.rmtree(scratch, ignore_errors=True)


def _record(script: dict, scratch: str) -> int:
    for agent in AGENTS:
        options = dict(agent=agent, corpus=LIBRARY, split="all", seed=0, tag=FIXTURE_TAG)
        recorded = execute_run(
            RunOptions(**options, mode="record", out=Path(scratch) / "rec" / agent),
            upstream=ScriptedUpstream(script),
        )
        replayed = execute_run(
            RunOptions(**options, mode="replay", out=Path(scratch) / "rep" / agent)
        )
        report = replayed.report
        if report.problems or report.stale_cassettes or report.verdict != "MEASURED":
            print(f"{agent}: replay is not faithful: {report.problems}", file=sys.stderr)
            return 1
        if _without_mode(comparable(recorded.report)) != _without_mode(comparable(report)):
            print(f"{agent}: replay differs from the recording", file=sys.stderr)
            return 1
        text = json.dumps(comparable(report), indent=2, sort_keys=True) + "\n"
        (FIXTURES / "expected" / f"{agent}.json").write_bytes(text.encode("utf-8"))
        print(f"{agent}: {report.n} cases x {report.k}, {len(report.runs)} runs recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
