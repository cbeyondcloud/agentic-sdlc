# Call flow — one `feature-delivery` run, start to finish

| | |
|---|---|
| Status | **Draft** (M1-73). Updated as components land; final version checked against the running system at the M1 demo (M1-72). |
| Scenario | Org mode, default `feature-delivery` pipeline, developer starts from a Jira ticket. Standalone mode is the same minus the control plane. |
| Legend | ✅ **built** — file and function exist and are tested · 🔲 **planned** — tracker row that builds it |

## 0. The pipeline being run

```mermaid
flowchart TD
    start([agentic run feature-delivery --from-jira PROJ-123]) --> pre[Preflight: policy, RBAC, kill switch,<br/>resolve every agent, handoff compatibility]
    pre -->|any violation| stop([Refused with reasons, nothing spent])
    pre --> ba[Step requirements<br/>Business Analyst → requirements]
    ba --> cond{requirements.size<br/>!= trivial ?}
    cond -->|yes| design[Step design<br/>Design agent → design]
    cond -->|no| g1
    design --> g1{{Gate design-approval<br/>team-lead}}
    g1 -->|reject + feedback| design
    g1 -->|approve| dev
    subgraph loop [Loop build-review, max 3 iterations]
        dev[Step develop<br/>Developer → dev-report<br/>in its own worktree + sandbox] --> rev[Step review<br/>pre-pass + Code Review → review]
        rev --> until{review.open_blocking == 0 ?}
        until -->|no, next iteration| dev
    end
    until -->|yes| g2{{Gate review-approval<br/>approver}}
    until -->|3 iterations used| g2
    g2 -->|reject + feedback| dev
    g2 -->|approve| qa[Step qa<br/>QA → test-report]
    qa --> pr[Action publish<br/>github.open_pr]
    pr --> done([Run closed: ledger sealed, cost summary, PR link])
```

## 1. Start and preflight

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant CLI as agentic CLI
    participant Core as Core (definitions + policy)
    participant CP as Control plane
    participant Jira
    participant TL as TokenLean

    Dev->>CLI: agentic run feature-delivery --from-jira PROJ-123
    CLI->>CLI: find repo root, load keys from OS keychain 🔲 M1-48
    CLI->>CP: fetch policy version + kill-switch state + CLI min version 🔲 M2-02 M2-06 M2-08
    CP-->>CLI: policy files (hash-pinned) or "use cache"
    CLI->>Core: load_policy(policy dir) ✅ policy/load.py
    Core-->>CLI: OrgPolicy + version (git:commit or sha256)
    CLI->>Core: DefinitionSet.discover(org, team, repo, allow_repo_pipelines) ✅ definitions/discovery.py
    Core-->>CLI: agents + pipelines (repo pipelines ignored unless allowed, D33)
    CLI->>Core: found.pipeline("feature-delivery") ✅
    CLI->>Jira: read PROJ-123 via connector mapping 🔲 M2-50
    Jira-->>CLI: ticket text (wrapped as untrusted data 🔲 M1-42)

    Note over CLI,Core: Preflight — nothing is spent until all checks pass 🔲 M1-25 M2-21
    loop every agent step in the pipeline
        CLI->>Core: resolve_agent(agent, policy, team, repo class) ✅ policy/resolve.py
        Core-->>CLI: ResolvedAgent (model, clamped limits, merged deny lists) or violations
    end
    CLI->>Core: check_compatibility(pipeline, agents) ✅ handoffs/compat.py
    CLI->>CP: RBAC run.trigger? concurrency slot? 🔲 M2-03 M2-08
    CLI->>TL: team budget remaining? 🔲 E2-09 (until then: skipped)
    CLI->>CLI: pipeline hash = approved version? shared skills hash-verified? 🔲 M2-21 M1-37
    CLI->>CLI: backends installed? sandbox available? 🔲 M1-27 M1-32 M1-33
    alt any check fails
        CLI-->>Dev: refused, one line per reason (what was asked, what is allowed)
    else --dry-run
        CLI-->>Dev: resolved plan: models, budgets, gates and approvers. Stops here. 🔲 M1-54
    else all checks pass
        CLI->>CLI: create run folder + run.json, ledger event run_opened 🔲 M1-21 M1-06
        CLI->>CP: ingest run_opened (queued offline if unreachable) 🔲 M2-20
    end
```

## 2. One agent step, in detail (same for every agent step)

```mermaid
sequenceDiagram
    autonumber
    participant Eng as Pipeline engine
    participant CP as Control plane
    participant Run as AgentRunner (Pydantic AI)
    participant Sec as Secret scan
    participant TL as TokenLean
    participant LLM as LLM provider
    participant WT as Worktree + sandbox
    participant Led as Run ledger

    Eng->>Eng: evaluate `when` over earlier handoffs 🔲 M1-23
    Eng->>CP: kill switch / pause for this team, user, agent, model? 🔲 M2-19
    Eng->>Led: event step_started 🔲 M1-06 M1-07
    Eng->>Run: run(ResolvedAgent, input handoffs) ✅ agents/runner.py PydanticAIRunner.run
    Run->>Run: build prompt = instructions + pre-loaded skills + AGENTS.md + input handoffs 🔲 M1-38 M1-46
    loop agent loop, until done or a limit is hit
        Run->>Sec: scan outgoing content for secrets 🔲 M1-41
        Run->>TL: model call with X-Agentic-* headers + traceparent (network transport → metering → identity client) ✅ agents/models.py metered_model, transport/client.py
        TL->>TL: routing rules (tier → model, team caps), PII and injection guardrails
        TL->>LLM: request
        LLM-->>TL: response
        TL-->>Run: response; cost read from body _token_opt or usage x policy price ✅ transport/cost.py cost_from_response (cost header: 🔲 ST-3)
        Run->>Run: budget ledger reserve/settle ✅ transport/budget.py, OTel span 🔲 M1-43
        opt model asks for a tool
            Run->>WT: read / search / edit / shell (allow-list, deny list, guards) ✅ agents/tools build_tools + commons Guard (sandbox 🔲 M1-32/33)
            WT-->>Run: result (wrapped as untrusted data) 🔲 M1-42
        end
        opt agent spawns a child agent
            Run->>Run: child gets a budget slice, narrower tools, parent's deny list; count/concurrency/depth capped per tree ✅ agents/children.py ChildSpawner
        end
        Run->>Run: stuck detector (on by default), turn and time limits (a stop kills running shell commands) ✅ agents/stuck.py + runner limits (engine timeouts 🔲 M1-26)
    end
    Run->>Run: answer checked against the kind's merged template (base + kind + org/team/repo extensions); framework stamps the envelope and counts open_blocking ✅ handoffs/contract.py HandoffContract, handoffs/finalise.py
    alt invalid handoff
        Run->>LLM: retry once with the validation errors (Pydantic AI output retry) ✅ agents/runner.py
        Run-->>Eng: still invalid: status invalid, last answer + errors kept → human gate 🔲 M1-22
    end
    Run-->>Eng: final handoff document
    Eng->>Led: event step_finished (digest of handoff, cost, tokens) ✅ sdlc_commons/ledger.py RunLedger.record
    Eng->>CP: ingest events + structured step log 🔲 M2-20 M1-47
```

## 3. A human gate (design-approval and review-approval)

```mermaid
sequenceDiagram
    autonumber
    participant Eng as Pipeline engine
    participant Led as Run ledger
    participant CP as Control plane
    actor Appr as Approver
    participant CLI as agentic CLI

    Eng->>Led: event gate_waiting (handoff digests pinned) 🔲 M1-22
    Eng->>CP: notify approvers (email) 🔲 M2-07
    Eng->>Eng: save run.json and exit the process (run is paused, nothing running) 🔲 M1-21
    Appr->>CLI: agentic status, then approve / reject --feedback / edit 🔲 M1-55
    CLI->>CP: does this person hold the gate's role? four-eyes rule? 🔲 M2-03
    alt edit
        CLI->>CLI: open handoff in $EDITOR, then parse_handoff again ✅ handoffs/parse.py
    end
    alt approve
        CLI->>Led: approval entry pinning the exact handoff digests (hash chain) 🔲 M1-22 M1-06
        CLI->>Eng: resume run from the next step 🔲 M1-54
    else reject with feedback
        CLI->>Led: rejection entry + feedback
        CLI->>CP: feedback record for the learning loop 🔲 M3-11
        CLI->>Eng: resume: re-run the rejected step with the feedback
    end
```

## 4. Build/review loop, QA, pull request, close

```mermaid
sequenceDiagram
    autonumber
    participant Eng as Pipeline engine
    participant WT as Worktree + sandbox
    participant Dev as Developer agent
    participant Rev as Code Review agent
    participant QA as QA agent
    participant GH as GitHub
    participant Led as Run ledger
    participant CP as Control plane

    loop iteration 1..3 of build-review 🔲 M1-24
        Eng->>WT: new git worktree from the last approved result 🔲 M1-30 M1-09
        Eng->>Dev: agent step (section 2) with design + previous review
        Dev->>WT: edit files, run tests (shell allow-list, deny list, protected paths) 🔲 M1-31
        Dev->>WT: commits with trailers (run, agent, approver) 🔲 M1-45
        WT-->>Eng: diff (checked against the diff-size budget 🔲 M2-24) + dev-report
        Eng->>Eng: pre-pass on the diff: gitleaks, ast-grep, osv-scanner 🔲 M2-48
        Eng->>Rev: agent step with dev-report + diff + pre-pass results
        Rev-->>Eng: review handoff (findings, open_blocking, score)
        Eng->>Eng: until review.open_blocking == 0 ? stuck / oscillating ? 🔲 M1-23 M1-18
    end
    Note over Eng: then gate review-approval (section 3)
    Eng->>QA: agent step: run tests in sandbox, check every acceptance criterion, dependency scan 🔲 M2-48
    QA-->>Eng: test-report
    Eng->>GH: open PR with run link + handoff links 🔲 M1-58
    GH-->>Eng: PR URL
    Eng->>Led: event run_closed, chain sealed 🔲 M1-06
    Eng->>CP: final events, cost summary per step and agent 🔲 M2-20 M1-57
```

**Running in the background during the whole run:** heartbeat to the control plane so a crashed run is detected 🔲 M3-07 · kill-switch polling at every step and turn 🔲 M2-19 · budget alerts at 80 % / 100 % 🔲 M2-16 · auto-trip on abnormal team spend or error rate 🔲 M2-11.

## 5. Step table

| # | When | Call | Where | Status |
|---|---|---|---|---|
| 1 | Start | Load org policy into `OrgPolicy`, compute version | `core/src/agentic/policy/load.py` · `load_policy`, `policy_version` | ✅ M1-03 |
| 2 | Start | Find agents and pipelines across org / team / repo, apply D33 | `core/src/agentic/definitions/discovery.py` · `DefinitionSet.discover` | ✅ M1-04 |
| 3 | Start | Read and validate each agent and pipeline file | `definitions/agent.py` · `load_agent`; `definitions/pipeline.py` · `load_pipeline` | ✅ M1-01 M1-02 |
| 4 | Preflight | Resolve each agent: model, clamped limits, merged deny lists, or violations | `core/src/agentic/policy/resolve.py` · `resolve_agent` | ✅ M1-04 |
| 5 | Preflight | Every step receives the handoffs it consumes | `core/src/agentic/handoffs/compat.py` · `check_compatibility` | ✅ M1-05 |
| 6 | Preflight | RBAC, kill switch, CLI version, concurrency, pipeline integrity, skills, backends, sandbox | engine preflight | 🔲 M1-25 M2-21 |
| 7 | Every step | Kill switch check, `when` evaluation | engine | 🔲 M2-19 M1-23 |
| 8 | Every step | Agent loop: prompt with the output template, model calls via TokenLean (metered), tools, child agents, limits, pause/resume; answer validated and retried once | `agentic.agents` (runner, metering, models, tools, children, stuck), `agentic.handoffs` (schema, extensions, finalise, contract) | ✅ M1-15…M1-20, M1-79 · 🔲 sandbox M1-32/33 |
| 9 | Every model call | Identity headers, corporate network, cost capture, budget ledger; secret scan, OTel | `agentic.transport` (client, network, cost, budget), `agentic.security` | ✅ M1-11…M1-14 · 🔲 M1-41 M1-43 |
| 10 | Every step end | Validate the agent's output as a handoff | `core/src/agentic/handoffs/parse.py` · `parse_handoff` | ✅ M1-05 |
| 11 | Every step end | Ledger entry with handoff digest; events to control plane | `sdlc_commons.ledger`, telemetry sinks | 🔲 M1-06 M2-20 |
| 12 | Gates | Pause, notify, approve / reject / edit with RBAC and four-eyes, digest-pinned approval | engine + CLI + control plane | 🔲 M1-22 M1-55 M2-03 M2-07 |
| 13 | Loop | Worktree per step, sandbox, shell policy, commits with trailers, diff budget | sandbox + commons | 🔲 M1-30…M1-33 M1-45 M2-24 |
| 14 | Review | Deterministic pre-pass before the review agent | security / review | 🔲 M2-48 |
| 15 | End | Open PR, seal ledger, cost summary | GitHub adapter, engine | 🔲 M1-58 M1-06 M1-57 |
| 16 | Background | Heartbeat, kill-switch polling, budget alerts, auto-trip | control plane + CLI | 🔲 M3-07 M2-19 M2-16 M2-11 |

**Built so far:** steps 1–5 and 10; the commons adapters (SP2); transport (SP3); the agent runtime (SP4): a resolved agent runs on Pydantic AI through TokenLean, every call metered against the budget, with guarded tools, child agents, stuck detection and pause/resume; and handoffs (SP5): every kind inherits a base template that org, team and repo layers may extend but never reduce (D37), and an agent's answer is validated, stamped and retried once. **Next to build:** the engine (SP6) that drives runners step by step.
