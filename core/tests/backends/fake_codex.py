"""Test helper: a stand-in for `codex exec --json` (never the real binary), so the codex
runner is tested on every OS. Run as `python fake_codex.py exec ...`.

It reads the plan file named by FAKE_CODEX_PLAN: `{"answers": [...], "log": "<path>"}`.
Each start takes the next answer (a counter file beside the plan) and logs its argv,
its stdin prompt and the CODEX_HOME it was given to the log (JSON lines). An answer:

    text   - the final agent message (default "done")
    usage  - turn.completed usage, or null for none (default: small counts)
    tools  - how many command_execution items to report first (default 0)
    sleep  - seconds to wait before answering (a slow model)
    child  - start a long-lived child process first (the time limit must kill it)
    fail   - report turn.failed with this message and exit 1
    early  - report an `error` with this message and exit 1 before any turn starts
    warn   - report a transient `error` event first (the turn still completes)
    tamper - overwrite CODEX_HOME's rules file, as a shell command in the step could
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    plan_path = Path(os.environ["FAKE_CODEX_PLAN"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    counter = plan_path.with_suffix(".count")
    index = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(index + 1))
    answers = plan["answers"]
    answer = answers[index] if index < len(answers) else {"text": "done"}
    prompt = sys.stdin.read()
    argv = sys.argv[1:]
    home = os.environ.get("CODEX_HOME")
    rules = Path(home or ".") / "rules" / "agentic.rules"
    seen = rules.read_text(encoding="utf-8") if rules.exists() else None
    with open(plan["log"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "prompt": prompt, "home": home, "rules": seen}))
        log.write("\n")
    if answer.get("tamper"):  # what a shell command inside the step could do
        rules.write_text('prefix_rule(pattern = ["git"], decision = "allow")\n', encoding="utf-8")
    if answer.get("warn"):  # a transient error Codex recovers from
        emit({"type": "error", "message": answer["warn"]})
    if answer.get("early"):  # Codex refusing to start (a config error): no turn began
        emit({"type": "error", "message": answer["early"]})
        return 1
    if answer.get("child"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path(plan["log"]).with_suffix(".child").write_text(str(child.pid))
    emit({"type": "thread.started", "thread_id": f"t-{index}"})
    emit({"type": "turn.started"})
    for n in range(answer.get("tools", 0)):
        item = {"id": f"c{n}", "type": "command_execution", "command": "ls", "status": "completed"}
        emit({"type": "item.started", "item": {**item, "status": "in_progress"}})
        emit({"type": "item.completed", "item": {**item, "aggregated_output": "", "exit_code": 0}})
        time.sleep(answer.get("tool_sleep", 0))
    time.sleep(answer.get("sleep", 0))
    if answer.get("fail"):
        emit({"type": "turn.failed", "error": {"message": answer["fail"]}})
        return 1
    text = answer.get("text", "done")
    emit({"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": text}})
    if "-o" in argv:
        Path(argv[argv.index("-o") + 1]).write_text(text, encoding="utf-8")
    usage = answer.get(
        "usage", {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20}
    )
    if usage is not None:
        emit({"type": "turn.completed", "usage": usage})
    return 0


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
