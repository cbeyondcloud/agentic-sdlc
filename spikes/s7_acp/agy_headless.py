"""Throwaway S7 probe: Antigravity CLI 1.3.0 headless (`-p --output-format json
--json-schema`) against the local Gemini-API stub, as a non-ACP alternative."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from stub import MARKER, ModelStub, RefusingProxy  # noqa: E402

OUT = Path(os.environ.get("S7_OUT", "/out"))
stub = ModelStub(str(OUT / "agy_headless_requests.jsonl"))
proxy = RefusingProxy()
root = Path("/s7/agy-headless")
home, work = root / "home", root / "work"
(home / ".gemini/antigravity-cli").mkdir(parents=True, exist_ok=True)
(home / ".gemini/antigravity-cli/settings.json").write_text(json.dumps({"modelProvider": "gemini", **json.loads(os.environ.get("S7_AGY_SETTINGS", "{}"))}))
work.mkdir(parents=True, exist_ok=True)
(work / "AGENTS.md").write_text(f"Project rule: {MARKER}\n")
(work / "GEMINI.md").write_text(f"Project rule: {MARKER}\n")
env = {
    "PATH": os.environ["PATH"], "HOME": str(home), "TERM": "dumb",
    "GEMINI_API_KEY": "s7-fake-key", "GOOGLE_GEMINI_BASE_URL": stub.url,
    "HTTPS_PROXY": proxy.url, "HTTP_PROXY": proxy.url, "NO_PROXY": "127.0.0.1,localhost",
}
schema = json.dumps({"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]})
results = {}
for label, extra in (("plain", []), ("schema", ["--json-schema", schema])):
    cmd = ["/opt/agy/antigravity", "-p", "Run this shell command: touch s7-tool-ran.txt",
           "--output-format", "json", "--print-timeout", "60s", *extra]
    try:
        p = subprocess.run(cmd, env=env, cwd=work, capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        results[label] = {"rc": p.returncode, "stdout": p.stdout[-2500:], "stderr": p.stderr[-1500:]}
    except subprocess.TimeoutExpired as exc:
        results[label] = {"timeout": True, "stdout": str(exc.stdout)[-800:]}
    results[label]["side_effect"] = (work / "s7-tool-ran.txt").exists()
results["requests"] = [
    {k: r.get(k) for k in ("method", "path", "answered_with", "marker_in_prompt")}
    | {"ua": r["headers"].get("user-agent", "")[:60]}
    for r in map(json.loads, Path(stub.log_path).read_text().splitlines())
] if Path(stub.log_path).exists() else []
results["proxy_attempts"] = proxy.attempts
(OUT / "agy-headless.json").write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=1)[:6000])
