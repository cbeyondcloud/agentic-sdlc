"""Throwaway S3 probe, part 3 (never imported by product code). Runs INSIDE the container.

Codex declares `apply_patch` as a `custom` (freeform, grammar) tool for catalog models. A
gateway that bridges to a chat-completions provider turns it into a function tool, so the
answer may come back as a `function_call`. Which item shapes does Codex accept?
  * custom_tool_call  {name: apply_patch, input: "<patch>"}       - the native shape
  * function_call     {name: apply_patch, arguments: {"input": ...}}
  * function_call     {name: apply_patch, arguments: {"content": ...}} - LiteLLM's bridge name
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402
import stub  # noqa: E402

PATCH = "*** Begin Patch\n*** Add File: s3-hello.txt\n+hello from s3\n*** End Patch\n"


def make_call(shape: str):
    def _call(tools, cmd):
        if shape == "custom":
            return {
                "type": "custom_tool_call",
                "id": "ctc_s3_1",
                "call_id": "call_s3_1",
                "name": "apply_patch",
                "input": PATCH,
                "status": "completed",
            }, "apply_patch"
        key = "input" if shape == "function_input" else "content"
        return {
            "type": "function_call",
            "id": "fc_s3_1",
            "call_id": "call_s3_1",
            "name": "apply_patch",
            "arguments": json.dumps({key: PATCH}),
            "status": "completed",
        }, "apply_patch"

    return _call


def main() -> None:
    probe.plant_user_home()
    results = {}
    for shape in ("custom", "function_input", "function_content"):
        stub._shell_call = make_call(shape)
        d = probe.run(f"T_apply_patch_{shape}", want_tool=True, model="gpt-5.5", schema=False)
        reqs = d["requests"]
        back = [
            i
            for r in reqs[1:2]
            for i in r["input_items"]
            if i["type"] in ("custom_tool_call_output", "function_call_output")
        ]
        results[shape] = {
            "exit": d["exit_code"],
            "n_requests": len(reqs),
            "returned_items": [(i["type"], i["output_head"]) for i in back],
            "events": d["codex_events"],
        }
        print(shape, json.dumps(results[shape])[:900])
    (probe.OUT / "probe3.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
