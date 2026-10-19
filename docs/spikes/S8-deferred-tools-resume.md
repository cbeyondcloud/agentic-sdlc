# Spike S8 — Pause at a gate in one process, resume in another

**Date:** 2026-10-19 · **Code (throwaway):** `spikes/s8_deferred_resume/spike.py` · **Version:** pydantic-ai-slim 2.54.0

## Question

Can a Pydantic AI run that stops for human approval be saved and finished by a **different process**? This is the gate model: `agentic approve` runs long after `agentic run` has exited.

## Method

A deterministic `FunctionModel` asks to call `write_file`, a tool marked `requires_approval=True`; the agent's output type is `[str, DeferredToolRequests]`. Process A runs until the deferred request, saves `ModelMessagesTypeAdapter.dump_json(result.all_messages())` plus the pending tool-call ids as JSON, and exits. Process B, a fresh interpreter started afterwards, loads the history and calls `run_sync(message_history=..., deferred_tool_results=DeferredToolResults(approvals={id: True | ToolDenied(reason)}))`.

## Results

| Check | Result |
|---|---|
| A stops with the call pending; the tool did not run in A | Yes: 1 pending call, no file written |
| The saved state is plain JSON | Yes: 1,311 bytes for one exchange |
| B approves → the tool runs in B and the model continues | Yes: `after the gate: wrote 5 chars to out.txt` |
| B denies → the tool never runs and the model sees the reason | Yes: `after the gate: refused at the design gate` |
| Usage across the pause | **Restarts** in B (1 request). The engine must save usage with the state and pass it back (`run(usage=...)`). |

## Decision

- **Mid-agent human approval** (a risky tool call, a BA question routed to a human) uses Pydantic AI deferred tools. The engine saves the message history (JSON), the pending call ids and the run usage in the run folder, and `agentic approve` / `reject` resumes in a new process.
- Gates **between steps** (design approval and so on) stay engine-level (M1-22); they don't need a paused agent.
- **The saved history is sensitive** (prompts and tool output). It lives in the git-ignored run folder (`.agentic/local/runs/`), never in the hash-chained ledger: the ledger records only its digest.
