# P0-04 Spike review (draft for owner sign-off)

Status: **signed off by the owner 2026-10-21** (Go; re-estimate, spec amendments and every recommendation in the owner-decision table accepted).

## Recommendation

**Go.** No spike found a blocker for M1. The decisions that changed are already in the spec (revision line) and the tracker. Five items need the owner (end of this page).

## Outcome per spike

| Spike | Question | Outcome | Decision / spec effect | Record |
|---|---|---|---|---|
| S1 | TokenLean cost capture through Pydantic AI | Works on both routes, streamed and not | D36: `httpx2`; cost capture is a wrapping transport | S1-cost-capture-pydantic-ai.md |
| S2 | Claude Code through TokenLean | Works, locked down | `claude-code` backend as an optional extra, `claude-agent-sdk` 0.2.164 | S2-claude-code-through-tokenlean.md |
| S3 | Codex `exec` through a Responses stub | Works end to end once locked down; Responses-only | ST-4 = Responses ingress adapter (requirements in record); M2-40 lock-down incl. mandatory untrusted worktree | S3-codex-through-tokenlean.md |
| S4 | Archon / OpenHands: build vs adopt | Build confirmed | D1/D17 confirmed (OpenHands later withdrawn as a backend by S7) | S4-build-vs-adopt.md |
| S5 | Code-graph bake-off | code-review-graph 2.3.9 wins | Default symbol adapter (M1-35) | S5-code-graph-bake-off.md |
| S6 | Windows sandbox | `srt` on every OS, Windows via WSL2 | D38 | S6-windows-sandbox.md |
| S7 | ACP + Antigravity | ACP v1 adopted as the single phase-2 adapter | **D32 revised**: opencode on ACP; Antigravity = `acp:antigravity` (conditional); §17 adds a Gemini route and path-prefix identity | S7-acp-and-antigravity.md |
| S8 | Deferred tools across processes | Works | Mid-agent approvals via deferred tools; history in the run folder, digest in the ledger | S8-deferred-tools-resume.md |
| S9 | Committed index in `main` | Do not commit the raw index | D39: local-first index; opt-in export outside `main` (M2-44) | S9-committed-index.md |
| S10 | Skill/agent A/B method | pydantic-evals + own judging/statistics layer | M1-59 counts raised; M1-63/M1-64 measurement fixed; M3-24 non-inferiority | S10-ab-eval-method.md |
| S11 | Serena (GPL) legal | Dropped | Condition never arose after S5 | de-scope-register.md |
| S12 | Docker Desktop at 1000 seats | Not needed; engine-neutral | `CONTAINER_ENGINE` in `scripts/check.py`; Rancher Desktop recommended | S12-container-runtime-licensing.md |
| S13 | Safe expression evaluator | cel-python | M1-23 | S13-expression-evaluator.md |
| S14 | Zyroflo reuse packaging | Pinned dependency + adapters | D34 | S14-zyroflo-reuse.md |

## Re-estimate (spike-driven deltas only)

M1 actuals are tracked per row in TRACKER.md; these are the changes the spikes add. All ±50 %.

| Row | Change | Delta (PW) |
|---|---|---|
| M1-59 | Larger eval corpus (S10) | +0.75 (applied) |
| M1-63 | Cassette server, `eval calibrate`, pre-registered A/B plans, stdlib statistics (S10) | +0.5 |
| M2-32 | Responses adapter scope now specified (S3): translation incl. custom tools | 0 (fits 1.5) |
| M2-40 | Org model catalog, Codex hooks evaluation (S3) | +0.5 |
| M2-57 | On the shared ACP backend (S7); builds `AcpBackend` | +0.25 |
| E2-06 | Shrinks to profiles on `AcpBackend` (S7) | −1 (phase 2) |
| SP19 (new P1) | TokenLean Gemini route, path-prefix identity (S7) | +1.5 (phase 2) |

Net for v1 (M1-M3): about **+2 PW** on ~170 PW, inside the estimate's error.

## Spec amendments applied tonight

- D1 note (S4, then S7), D32 revision (S7), §9.1 `opencode` and `acp:<agent>` rows (S7), §17 two P1 TokenLean items (S7), revision line updated.

## Owner decisions this review needs

| Item | Question | Recommendation |
|---|---|---|
| D32 revision (decision 27) | Move opencode onto the shared ACP backend? | Yes |
| DF-102 | Buy Docker Desktop Business seats (~$288k/yr list)? | No, not for the framework |
| DF-103 | Accept EV-signed installers bundling unsigned Apache-2.0 helpers? | Yes, or re-sign with the org certificate |
| DF-120 / DF-121 | Legal: Antigravity terms; Copilot BYOK seat need | Legal review before E2-06 |
| DF-60 / DF-61 | Run `python -m agentic.sandbox.probe` in WSL2 and on a Mac | Owner action |

All overnight decisions, with alternatives and reasons: `DECISIONS-overnight-2026-10-21.md`.
