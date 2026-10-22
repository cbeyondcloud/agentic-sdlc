# M1-64 agent quality gate: live run (owner runbook)

The quality gate (tracker row M1-64) measures the five built-in agents against live models.
- **Model access:** one OpenRouter key (decision 63).
- **Tier mapping:** Claude agents with a GPT judge (decision 64).
- **Method:** spike S10 / D41.
- **Where it runs:** inside WSL2, in the Debian distro, so the code the Developer agent writes runs in
  the `srt` sandbox that DF-60 proved there (decision 69). TokenLean LITE runs in the same distro
  on Docker Engine, so nothing needs Windows networking or Docker Desktop.

**What it costs:** about $250 at OpenRouter list prices for the whole gate (S10 estimate).
Each agent's run is capped by `--budget-usd` (decision 20).

## Owner steps (two)

**1. Put your OpenRouter key in TokenLean's file.** Open this file in Notepad, set
`LLM_KEY_OPENROUTER=` to your key, and save. The key stays in this file inside WSL. Nothing
in agentic-sdlc reads or copies it.

```powershell
notepad \\wsl.localhost\Debian\home\dev\token-optimisation\deploy\lite\local\.env.lite
```

**2. Tell the coordinator "go".** That starts the paid run. The coordinator then runs the
steps below, and writes the gate report from `reports/m1-64/*/report.json`.

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

# the gate: five agents, each capped; record mode keeps every answer for free replays
policy=~/.agentic/eval-policy-m1-64
for pair in business-analyst:100 design:100 code-review:100 qa:250 developer:750; do
  agent=${pair%%:*}; cap=${pair##*:}
  uv run agentic eval run --agent "$agent" --mode record --gateway http://127.0.0.1:4100 \
    --policy "$policy" --agents library/agents --corpus library/eval \
    --cassettes ~/.agentic/eval-m1-64/cassettes/"$agent" \
    --budget-usd "$cap" --out reports/m1-64/"$agent"
done

# afterwards
cd ~/token-optimisation/deploy/lite/local && ./down.sh
```

## If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `no provider key in .env.lite` | The key was not saved | Repeat owner step 1 |
| A cost of `$0.000000` in the smoke test | TokenLean has no price for the model yet | Fine: agentic-sdlc estimates from `docs/runbooks/m1-64/prices.yaml` |
| `model_not_configured` (400) | The model name is not served | Check the name in `docs/runbooks/m1-64/tiers.yaml` against OpenRouter |
| `budget` stop in a report | The cap was reached | Expected protection; the report counts the cases that finished |
| `judge_same_family` | The judge shares the agents' family | `make_policy.py` refuses this; check `tiers.yaml` |
