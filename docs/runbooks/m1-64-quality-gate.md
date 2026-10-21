# M1-64 agent quality gate: live run (owner runbook)

The quality gate (tracker row M1-64) measures the five built-in agents against live models.
Model access is one OpenRouter key (decision 63). The tier mapping is Claude agents with a
GPT judge (decision 64). The method is spike S10 / D41.

**What it costs:** about $250 at OpenRouter list prices for the whole gate (S10 estimate).
Each agent's run is capped by `--budget-usd` (decision 20), so it stops before overspending.

**What you need:** your OpenRouter key, Docker or Podman running, and about 30 minutes.
Run every step in **PowerShell**, one at a time.

---

## Part A: TokenLean LITE with your key

**A1. Start LITE once.** The first start creates `.env.lite` with generated secrets.

```powershell
cd D:\token-optimisation-wt\sp19-lite\deploy\lite\local
.\up.ps1
```

**A2. Add your OpenRouter key.** In the file that opens, set `LLM_KEY_OPENROUTER=` to your
key and save. The key stays in this file on your machine. Nothing in agentic-sdlc ever
reads or copies it.

```powershell
notepad .env.lite
```

**A3. Restart LITE so it picks up the key.**

```powershell
.\down.ps1; .\up.ps1
```

**A4. Smoke-test a real, cheap call through OpenRouter.**

```powershell
$env:SMOKE_MODEL = "openrouter/anthropic/claude-haiku-5.5"; .\smoke.ps1
```

Expect `5/5 PASS`.
- A cost of `$0.000000` means TokenLean has no price for that model yet. The run still
  works, because agentic-sdlc estimates the cost from `docs/runbooks/m1-64/prices.yaml`.
- `model not substituted` must appear.

## Part B: the quality gate

**B1. Give the agentic CLI your local TokenLean key** (the per-user key `up.ps1` generated,
not your OpenRouter key).

```powershell
$env:AGENTIC_TOKENLEAN_API_KEY = ((Select-String -Path .env.lite -Pattern '^TOKENLEAN_API_KEY=').Line -split '=', 2)[1]
cd D:\agentic-sdlc
```

**B2. Build the eval policy.** This is the template plus your tier mapping. It checks that
the judge is from a different family than the agents.

```powershell
uv run python docs/runbooks/m1-64/make_policy.py
```

**B3. Run the gate.** This runs the five agents one after another, each with its spend
cap. It takes a while; the reports go under `reports\m1-64\`.

```powershell
$policy = "$env:USERPROFILE\.agentic\eval-policy-m1-64"
$caps = @{ "business-analyst" = 100; "design" = 100; "code-review" = 100; "qa" = 250; "developer" = 750 }
foreach ($agent in $caps.Keys) {
  uv run agentic eval run --agent $agent --mode record --gateway http://127.0.0.1:4100 `
    --policy $policy --agents library/agents --corpus library/eval `
    --cassettes "$env:USERPROFILE\.agentic\eval-m1-64\cassettes\$agent" `
    --budget-usd $caps[$agent] --out "reports\m1-64\$agent"
}
```

`--mode record` also saves every answer, so the same run can be replayed later for free.

**B4. Tell the coordinator it is done.** It reads `reports\m1-64\*\report.json`, writes
the gate report against the M1-64 targets, and stops LITE when you say so
(`.\down.ps1` in `deploy\lite\local`).

---

## If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `no provider key in .env.lite` (A4) | The key was not saved, or LITE was not restarted | Repeat A2 and A3 |
| `model_not_configured` (400) | The model name is not served | Check the name in `docs/runbooks/m1-64/tiers.yaml` against OpenRouter |
| `budget` stop in a report | The cap was reached | Expected protection; the report still counts the cases that finished |
| `judge_same_family` in a report | The judge shares the agents' family | B2 refuses this; re-run B2 |
