"""Print a compact view of one or more probe results: python show.py out/gemini.json ..."""

import json
import sys

for path in sys.argv[1:]:
    d = json.load(open(path, encoding="utf-8"))
    init = d.get("initialize") or {}
    sess = d.get("new_session") or {}
    print("=" * 30, d["agent"], d.get("seconds"), "s", "ERROR:" if d.get("error") else "", d.get("error") or "")
    print("agentInfo:", init.get("agentInfo"), "| protocol:", init.get("protocolVersion"))
    print("caps:", json.dumps(init.get("agentCapabilities"))[:400])
    print("authMethods:", [m.get("id") for m in init.get("authMethods") or []], "| authenticate:", d.get("authenticate"))
    print("modes:", [m["id"] for m in (sess.get("modes") or {}).get("availableModes", [])],
          "| configOptions:", [(o.get("id"), o.get("category"), o.get("currentValue")) for o in sess.get("configOptions") or []],
          "| models:", json.dumps(sess.get("models"))[:300] if sess.get("models") else None)
    print("stop:", (d.get("prompt_response") or {}).get("stopReason"), "| meta:", json.dumps((d.get("prompt_response") or {}).get("_meta"))[:300])
    print("permissions asked:", [(p["tool_call"].get("title"), [o["kind"] for o in p["options"]]) for p in d["permissions"]])
    print("tool side effect happened:", d.get("tool_side_effect_file_exists"))
    print("updates:", d["updates"]["kinds"], "| text:", repr(d["updates"]["agent_text"][:160]))
    print("tool events:", d["updates"]["tool_events"][:6])
    print("usage updates:", d["updates"]["usage"][:2])
    print("client calls:", d["client_calls"][:5])
    for r in d["model_requests"]:
        print("  REQ", r["method"], r["path"], "model=", r["model"], "ans=", r.get("answered_with"),
              "marker=", r["marker_in_prompt"], "hdrs=", r["agentic_headers"], "auth=", r["auth_header"],
              "ua=", r["user_agent"][:50], "ntools=", len(r.get("tools") or []))
    tools = next((r.get("tools") for r in d["model_requests"] if r.get("tools")), None)
    print("tools offered:", tools)
    print("proxy attempts:", d["proxy_attempts"])
    print("stderr tail:", d["stderr_tail"][-600:].replace("\n", " | "))
