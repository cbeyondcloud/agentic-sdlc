# De-scope register

Anything dropped from the plan is recorded here (Zyroflo convention), so the
tracker row can be marked `Dropped` with a traceable reason.

| Date | Tracker ID | What was dropped | Why | Decided by | Revisit when |
|---|---|---|---|---|---|
| 2026-10-20 | S11 | Spike: Serena (GPL) as a separate process - legal review | Its trigger never happened: S5 chose code-review-graph (MIT) as the symbol adapter, so no LSP-based tool (multilspy or Serena) is needed; a GPL dependency would also be outside the licence allow-list | Claude (overnight autonomy; decision 10 in DECISIONS-overnight-2026-10-21.md), for owner review | A language the default adapter cannot index well enough is required |
| 2026-10-20 | DF-98 (S4) | OpenHands as an optional `acp:openhands` backend | Spike S7: the OpenHands CLI is no longer maintained, runs on Windows only under WSL, and its ACP mode ignores env routing and stores the API key in plain text | Claude (overnight autonomy; decision 26), for owner review | OpenHands ships a maintained ACP agent with env-based configuration |
