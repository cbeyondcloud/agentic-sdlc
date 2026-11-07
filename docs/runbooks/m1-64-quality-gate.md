# M1-64 agent quality gate: live run (owner runbook)

The quality gate (tracker row M1-64) measures the five built-in agents against live models.
- **Model access:** one OpenRouter key (decision 63).
- **Tier mapping:** Claude agents with a GPT judge (decision 64).
- **Method:** spike S10 / D41.
- **Where it runs:** inside WSL2, in the Debian distro, so the code the Developer agent writes runs in
  the `srt` sandbox that DF-60 proved there (decision 69). TokenLean LITE runs in the same distro
  on Docker Engine, so nothing needs Windows networking or Docker Desktop.

**What it may cost:** at most **$250 for the whole gate, reruns included** (owner decision
2026-11-07, DF-108; S10 estimated about $245). See [Spend caps](#spend-caps) below.

## Owner steps (two)

**1. Put your OpenRouter key in TokenLean's file.** Open this file in Notepad, set
`LLM_KEY_OPENROUTER=` to your key, and save. The key stays in this file inside WSL. Nothing
in agentic-sdlc reads or copies it.

```powershell
notepad \\wsl.localhost\Debian\home\dev\token-optimisation\deploy\lite\local\.env.lite
```

**2. Tell the coordinator "go".** That starts the paid run. The coordinator then runs the
steps below, and writes the gate report from each agent's latest
`reports/m1-64/<agent>/<attempt>/report.json`.

## What the coordinator runs (user `dev` in the Debian WSL distro)

Shown here so anyone can repeat the run.

```bash
# TokenLean LITE with the key, and one cheap real call through OpenRouter
cd ~/token-optimisation/deploy/lite/local
./up.sh
SMOKE_MODEL=openrouter/anthropic/claude-haiku-5.5 ./smoke.sh   # expect 5/5 PASS, model not substituted

# the agentic CLI uses LITE's per-user key (not the OpenRouter key)
export AGENTIC_TOKENLEAN_API_KEY="$(sed -n 's/^TOKENLEAN_API_KEY=//p' .env.lite)"

# the eval policy: template + tier mapping, refused if the judge shares the agents' family
cd ~/agentic-sdlc
uv run python docs/runbooks/m1-64/make_policy.py

# the gate: five agents, each capped at what its share has left (earlier attempts and
# reruns count); record mode keeps every answer for free replays. The loop stops at the cap.
policy=~/.agentic/eval-policy-m1-64
for agent in business-analyst design code-review qa developer; do
  cap=$(uv run python scripts/live_caps.py remaining m1-64-gate --agent "$agent" \
        --spent-in reports/m1-64) || break
  uv run agentic eval run --agent "$agent" --mode record --gateway http://127.0.0.1:4100 \
    --policy "$policy" --agents library/agents --corpus library/eval \
    --cassettes ~/.agentic/eval-m1-64/cassettes/"$agent" \
    --budget-usd "$cap" --out reports/m1-64/"$agent"/"$(date -u +%Y%m%dT%H%M%SZ)"
done

# afterwards
cd ~/token-optimisation/deploy/lite/local && ./down.sh
```

## Spend caps

Owner decision 2026-11-07 (DF-108). The caps live in
[`live-spend-caps.yaml`](live-spend-caps.yaml); `scripts/live_caps.py` reads them, and
`make_policy.py` writes the gate's per-agent shares into the eval policy (`eval.yaml`).

| Run | Ceiling (reruns included) | How it is enforced |
|---|---|---|
| First live acceptance run (scenario 1, M1-68) | $20 | `agentic run feature-delivery --budget "$(uv run python scripts/live_caps.py remaining acceptance-1 --spent-in <sample repo>/.agentic/local)"`; at the cap the run pauses at `budget` (exit 5) and reports |
| Full agent quality gate (M1-64) | $250 | the loop above: BA, Design and Code Review $19.23 each, QA $48.08, Developer $144.23 (S10's placeholder A/B caps of 100/100/100/250/750, scaled to $250); at the cap an evaluation stops as `INCOMPLETE` and the loop stops |
| Outer backstop | ~$300 | set a credit limit of about **$300** on the provider key itself (OpenRouter: the key's credit limit). It holds even if every cap above were bypassed |

`live_caps.py remaining` sums what every earlier attempt recorded (eval `report.json` in live
and record mode, each run's `run.json` budget), so a rerun gets only what is left; it exits 1
with "cap reached" when nothing is. Each attempt writes a new folder, so none is overwritten.
A run's `--extra-budget` or `--add-budget` on resume stays within the cap only if the amount
comes from `live_caps.py remaining` too.

## If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `no provider key in .env.lite` | The key was not saved | Repeat owner step 1 |
| A cost of `$0.000000` in the smoke test | TokenLean has no price for the model yet | Fine: agentic-sdlc estimates from `docs/runbooks/m1-64/prices.yaml` |
| `model_not_configured` (400) | The model name is not served | Check the name in `docs/runbooks/m1-64/tiers.yaml` against OpenRouter |
| `budget` stop in a report | The cap was reached | Expected protection; the report counts the cases that finished |
| `cap reached` from `live_caps.py` | The campaign's ceiling is spent (reruns included) | Stop and report; only the owner raises a cap, in `live-spend-caps.yaml` |
| `judge_same_family` | The judge shares the agents' family | `make_policy.py` refuses this; check `tiers.yaml` |
