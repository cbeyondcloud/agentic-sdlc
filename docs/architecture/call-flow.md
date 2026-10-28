# Call flow — one `feature-delivery` run, start to finish

| | |
|---|---|
| Status | **Updated 2026-10-21** (M1-73) to match the code on `m1-foundation` plus the M1 finishing track. The final check against the running system is at the M1 demo (M1-72). |
| Scenario | **Standalone mode**, which is what M1 delivers (spec §24): one person, a local policy (`agentic init --standalone`), any TokenLean gateway, the library `feature-delivery` pipeline. Org mode adds the control plane: every box marked M2-xx. |
| Legend | ✅ **built**: the code exists and is tested, named as `package/module.py · function`. 🔲 **planned**: the tracker row that builds it. |
| Paths | `agentic/…` is `core/src/agentic/`; `agentic_cli/…` is `cli/src/agentic_cli/`; `sdlc_commons/…` is `commons/src/sdlc_commons/`, the adapters over Zyroflo. The core never imports `sdlc_commons` or Zyroflo; `agentic_cli/runtime.py` and `agentic_cli/wiring.py` join them. |
| Diagrams | Checked with mermaid-cli 11.4.2 in its Linux container: `uv run python scripts/check_diagrams.py docs/architecture/call-flow.md`. |

## 0. The pipeline being run

`library/pipelines/feature-delivery.yaml` runs in the `team` regime: agent commands need the OS sandbox, and every gate needs a person.

```mermaid
flowchart TD
    start(["agentic run feature-delivery --input request=..."]) --> pre["Preflight: policy, resolve every agent,<br/>handoff compatibility, skills, sandbox, backends"]
    pre -->|any problem| stop(["Refused with reasons, nothing spent (exit 3)"])
    pre --> ba["Step requirements<br/>business-analyst → requirements"]
    ba --> g0{{"Gate requirements-approval<br/>team-lead"}}
    g0 -->|reject + answers| ba
    g0 -->|approve| base["Step baseline<br/>qa-baseline → test-report on the untouched tree"]
    base --> design["Step design<br/>design → design"]
    design --> g1{{"Gate design-approval<br/>team-lead"}}
    g1 -->|reject + feedback| design
    g1 -->|approve| dev
    subgraph loop ["Loop build-review, at most 3 iterations"]
        dev["Step develop<br/>developer → dev-report<br/>in the run's worktree + sandbox"] --> rev["Step review<br/>code-review → review"]
        rev --> qa["Step qa (every round)<br/>qa → test-report against the baseline"]
        qa --> until{"review.open_blocking == 0<br/>and test_report.verdict == 'pass' ?"}
        until -->|no: review and QA report go back| dev
    end
    until -->|yes| g2{{"Gate review-approval<br/>approver"}}
    until -->|"3 iterations used, or oscillating"| gx{{"Gate build-review-exhausted<br/>approve refused while until is false"}}
    gx -->|"edit: accept a finding, or name a test in accepted_failures"| g2
    gx -->|reject + feedback| dev
    g2 -->|reject + feedback| dev
    g2 -->|approve| pr["Action publish<br/>github.open_pr (M1-58)"]
    pr --> last{"a newest handoff reports a failure ?"}
    last -->|no| done(["Run closed: run_closed event, cost by step"])
    last -->|yes| failing(["Paused failing-handoff: the failing handoffs pinned for agentic show"])
```

## 1. Setup, start and preflight

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant CLI as agentic CLI
    participant RT as Runtime (agentic_cli)
    participant Core as Core: policy, definitions, preflight
    participant CP as Control plane

    Dev->>CLI: agentic init --standalone (once per repo and machine)
    CLI->>CLI: repo-root and safety checks ✅ agentic_cli/repo_guard.py · check_repo
    CLI->>CLI: no --policy: install the template once into ~/.agentic/policy/standalone ✅ agentic_cli/policy_source.py · resolve_policy, agentic_cli/standalone.py · assemble
    CLI->>CLI: prerequisites, sandbox readiness (WSL2 steps on native Windows), skills sync, tools, local index, readiness report ✅ agentic_cli/dev_init.py · run_dev_init
    Dev->>CLI: agentic run feature-delivery --input request=... [--budget USD]
    CLI->>RT: Workspace.load: repo, policy repo, team, person ✅ agentic_cli/workspace.py · Workspace
    RT->>RT: gateway URL + key (env, then OS keyring) ✅ agentic_cli/runtime.py · Runtime.gateway, agentic/auth/credentials.py · Credentials
    CLI->>CP: policy version, kill switch, minimum CLI version 🔲 M2-02 M2-06 M2-08
    RT->>Core: load_policy(policy dir) ✅ agentic/policy/load.py · load_policy
    RT->>Core: DefinitionSet.discover(org, team, repo) ✅ agentic/definitions/discovery.py · DefinitionSet.discover
    Note over RT,Core: Preflight: nothing is spent until every check passes ✅ agentic/engine/preflight.py · preflight, called by agentic_cli/runtime.py · Runtime.preflight
    loop every agent step
        Core->>Core: resolve_agent: model, clamped limits, merged deny lists, or violations ✅ agentic/policy/resolve.py · resolve_agent
        Core->>Core: skills in the catalog and installed (synced first) ✅ agentic/engine/preflight.py · skill_problems, agentic/skills/install.py · sync_skills
    end
    Core->>Core: handoff compatibility, conditions, actions, backends ✅ agentic/handoffs/compat.py · check_compatibility
    Core->>Core: sandbox decision by regime ✅ agentic/sandbox/readiness.py · decide_sandbox
    CLI->>CP: RBAC run.trigger, concurrency slot, team budget, pipeline integrity 🔲 M2-03 M2-08 M2-21 E2-09
    alt a problem
        CLI-->>Dev: refused, one line per problem (exit 3)
    else --dry-run
        CLI-->>Dev: resolved plan: models, budgets, gates, sandbox, nothing spent ✅ agentic_cli/runtime.py · Runtime.plan
    else all checks pass
        RT->>RT: build the engine: runners, ledgers, step logs, redactor ✅ agentic_cli/runtime.py · Runtime.engine
        CLI->>Core: Engine.start: run.json, run ledger opened ✅ agentic/engine/engine.py · Engine.start, agentic/engine/state.py · RunStore.save, sdlc_commons/ledger.py · RunLedger.open
        CLI->>CP: ingest run events (queued offline) 🔲 M2-20
    end
```

A request is given as `--input request=...`, or taken from a Jira issue with `agentic run <pipeline> --from-jira PROJ-123` ✅ M2-50 (agentic_cli/jira.py · the `jira:` user settings in docs/user-config.md); the run reports back to the issue as it ends.

## 2. One agent step (the same for every agent step)

```mermaid
sequenceDiagram
    autonumber
    participant Eng as Engine
    participant Wire as StepWiring + StepRunners
    participant Run as PydanticAIRunner
    participant Sec as Secret scan
    participant TL as TokenLean
    participant LLM as Model provider
    participant WT as Worktree + sandbox
    participant Led as Run ledger + logs

    Eng->>Eng: evaluate `when` over earlier handoffs (CEL) ✅ agentic/engine/expressions.py · compile_condition
    Eng->>Eng: kill switch for team, user, agent, model? 🔲 M2-19
    Eng->>Led: open the step log (JSON lines, redacted) ✅ agentic/telemetry/logs.py · StepLogs.step
    Eng->>Eng: resolve the agent again, open the step's budget slice ✅ agentic/engine/engine.py · _Run._agent, agentic/transport/budget.py · BudgetSlice.open
    Eng->>WT: checkpoint the tree before the step ✅ sdlc_commons/worktrees.py · StepCheckpoints.begin
    Eng->>Led: step_started ✅ sdlc_commons/ledger.py · RunLedger.record
    Eng->>Wire: runner for the step ✅ agentic/engine/factory.py · StepRunners
    Wire->>Wire: tool context: tree, guard from the agent's allow/ask/deny lists, srt sandbox or a plain process (solo only), skills, codeintel ✅ agentic_cli/wiring.py · StepWiring.tools_for, sdlc_commons/guards.py · Guard.for_agent
    Wire->>Run: native backend: metered model, tools, child spawner ✅ agentic/backends/native.py · NativeBackend.runner
    Eng->>Run: run(instructions + AGENTS.md + skill block + input handoffs + output template) ✅ agentic/agents/runner.py · PydanticAIRunner.run, agentic/memory/agents_md.py · agents_md_knowledge, agentic/skills/loader.py · SkillPromptRunner
    loop the agent loop, until an answer or a limit
        Run->>Sec: scan the outgoing request: block or mask, audit event ✅ agentic/transport/scan.py · ScanningTransport, agentic/security/secret_scan.py · SecretScanner
        Run->>Run: reserve the call's estimate on the budget slice ✅ agentic/agents/metering.py · MeteringTransport
        Run->>TL: model call with X-Agentic-* identity headers + traceparent ✅ agentic/transport/client.py · make_async_client
        TL->>LLM: routed by tier, TokenLean guardrails
        LLM-->>TL: response
        TL-->>Run: response with its cost (x-tokenlean-cost-usd, body, or usage x policy price) ✅ agentic/transport/cost.py · cost_from_response
        Run->>Run: settle the reservation, OTel gen_ai span ✅ agentic/transport/budget.py · Reservation.settle, agentic/telemetry/spans.py · mark_call
        opt the model asks for a tool
            Run->>WT: read, search, edit, shell: guard first, then the sandbox ✅ agentic/agents/tools/__init__.py · build_tools, agentic/agents/tools/shell.py · shell_tools, agentic/sandbox/srt.py · SrtSandbox.run
            WT-->>Run: result with host paths scrubbed, secrets masked, fenced as untrusted data ✅ agentic/agents/tools/protect.py · protect_tools, agentic/security/untrusted.py · untrusted
            Run->>Run: an ask-listed command pauses the run for a person ✅ agentic/agents/runner.py · PydanticAIRunner._pause
        end
        opt the agent spawns a child agent
            Run->>Run: budget slice, narrower tools, the parent's deny list, count/concurrency/depth caps ✅ agentic/agents/children.py · ChildSpawner.spawn
        end
        Run->>Run: stuck check, turn and time limits ✅ agentic/agents/stuck.py · stuck_check
    end
    Run->>Run: answer checked against the kind's merged template, retried once with the errors ✅ agentic/handoffs/contract.py · HandoffContract, agentic/handoffs/finalise.py · finalise_handoff
    Run-->>Eng: outcome: completed, invalid, paused or stopped
    Eng->>WT: checkpoint commit with audit trailers ✅ agentic/telemetry/trailers.py · commit_message, sdlc_commons/worktrees.py · StepCheckpoints.finish
    Eng->>Led: handoff written and pinned by digest, step_finished with its cost ✅ sdlc_commons/ledger.py · RunLedger.write_artifact, RunLedger.record
    Eng->>Eng: run.json saved before the next step ✅ agentic/engine/state.py · RunStore.save
    Eng->>Eng: forward events to the control plane 🔲 M2-20 (sink seam ✅ agentic/telemetry/sinks.py · ForwardingRecorder)
```

What stops a step, and what happens next (✅ `agentic/engine/engine.py · _Run._agent`, `_Run._outcome`):

| Cause | Run state | Next |
|---|---|---|
| Run or step budget reached | paused `budget` / `step-budget` | `agentic resume --extra-budget` (run budget only) |
| Turns, time, stuck agent | paused `step-turns` / `step-time` / `step-stuck` | `agentic resume` |
| Invalid answer after one retry | waiting at gate `invalid-output` | `agentic edit` or `agentic reject` |
| Agent reports it is blocked | waiting at gate `blocked-output` | a person decides |
| A command on the ask list | paused for approval | `agentic resume --allow/--deny` |
| Policy refuses the agent, provider or tool error | paused `policy` / `step-error` | fix, then `agentic resume` |
| Secret found in outgoing content (block) | paused `step-error`, audit event | remove the secret, `agentic resume` |

## 3. A human gate

```mermaid
sequenceDiagram
    autonumber
    participant Eng as Engine
    participant Gat as Gate ledger (Zyroflo)
    participant CP as Control plane
    actor Appr as Approver
    participant CLI as agentic CLI

    Eng->>Gat: gate_waiting, handoff digests pinned ✅ agentic/engine/engine.py · _Run._wait, sdlc_commons/gates.py · ZyrofloGates.waiting
    Eng->>Eng: run.json saved, the process exits (exit 4) ✅ agentic_cli/driving.py · exit_code_for
    Eng->>CP: notify approvers by email 🔲 M2-07
    Appr->>CLI: agentic waiting / show / status ✅ agentic_cli/commands/gates.py
    Appr->>CLI: agentic logs run --follow (while a run is running) ✅ agentic_cli/commands/runs.py · logs, agentic/telemetry/logs.py · LogTail
    Appr->>CLI: approve, or reject --feedback, or edit ($EDITOR) ✅ agentic_cli/commands/gates.py · approve, reject, edit
    CLI->>Eng: Engine.decide ✅ agentic/engine/engine.py · _Run.decide
    Eng->>Eng: role check (standalone: every role held), four-eyes in regulated or four_eyes gates ✅ agentic/engine/gates.py · StandaloneAuthorizer, sdlc_commons/gates.py · self_approval_refusal
    CLI->>CP: org roles and authenticated approvers 🔲 M2-03
    alt edit
        Eng->>Eng: edited handoff validated, original kept, repinned ✅ agentic/engine/engine.py · _Run._edit
    end
    alt approve
        Eng->>Gat: approval entry pinning the exact digests (hash chain) ✅ agentic/engine/engine.py · _Run._approve, sdlc_commons/gates.py · ZyrofloGates.record
        Eng->>Eng: advance from the next step
    else reject with feedback
        Eng->>Gat: rejection entry with the feedback ✅ agentic/engine/engine.py · _Run._reject
        Eng->>Eng: roll the tree back, re-run the rejected step with the feedback ✅ sdlc_commons/worktrees.py · StepCheckpoints.rollback
        Eng->>CP: feedback record for the learning loop 🔲 M3-11
    end
```

## 4. Build/review loop, QA, pull request, close

```mermaid
sequenceDiagram
    autonumber
    participant Eng as Engine
    participant WT as Worktree + sandbox
    participant Dev as developer
    participant Rev as code-review
    participant QA as qa
    participant GH as GitHub
    participant Led as Run ledger

    loop iteration 1..3 of build-review ✅ agentic/engine/engine.py · _Run._loop
        Eng->>Dev: agent step (section 2) with design, test-report, previous review
        Dev->>WT: edit files, run tests (allow list, deny list, protected paths) ✅ sdlc_commons/guards.py · Guard.command
        WT-->>Eng: checkpoint commit with trailers + dev-report ✅ agentic/telemetry/trailers.py · commit_message
        Eng->>Eng: diff-size budget per review batch 🔲 M2-24
        Eng->>Eng: deterministic pre-pass: gitleaks, ast-grep, osv-scanner 🔲 M2-48
        Eng->>Rev: agent step with dev-report and the diff
        Rev-->>Eng: review handoff: findings, open_blocking, score
        Eng->>QA: agent step, every round: tests against the baseline, every acceptance criterion
        QA-->>Eng: test-report with verdict
        Eng->>Eng: until review.open_blocking == 0 && test_report.verdict == 'pass', else oscillation check ✅ agentic/engine/engine.py · _Run._loop, _Run._oscillating
        Eng->>Led: loop_back: the review and QA report go to the Developer ✅ sdlc_commons/ledger.py · RunLedger.record
    end
    Note over Eng: exhausted: gate build-review-exhausted, where approve is refused while until is false - edit (accept a finding, or name a test in accepted_failures), reject, or stop ✅ agentic/engine/engine.py · _Run._loop_still_failing
    Note over Eng: then gate review-approval (section 3)
    Eng->>GH: push the run's branch, open the PR with run and handoff links 🔲 M1-58 (PR body ✅ agentic/telemetry/trailers.py · pr_body)
    Eng->>Eng: a newest handoff still reports a failure: pause failing-handoff, failing handoffs pinned ✅ agentic/engine/engine.py · _Run.go, _Run._failures
    Eng->>Led: run_closed ✅ agentic/engine/engine.py · _Run._close
    Eng-->>Eng: agentic cost: by step, agent and child ✅ agentic_cli/commands/cost.py
```

**Running beside the whole run:** structured step logs in `<run>/logs/` and OTel spans in the run folder ✅ `agentic_cli/driving.py · drive, traces_into` · run heartbeat and zombie detection 🔲 M3-07 · kill-switch polling at every step and turn 🔲 M2-19 · budget alerts at 80 % and 100 % 🔲 M2-16 · auto-trip on abnormal team spend or error rate 🔲 M2-11.

## 5. Step table

| # | When | Call | Where | Status |
|---|---|---|---|---|
| 1 | Setup | Standalone policy repo from the template, installed once | `agentic_cli/policy_source.py · resolve_policy`, `agentic_cli/standalone.py · assemble` | ✅ M1-62 |
| 2 | Setup | Readiness: prerequisites, sandbox (WSL2 guidance), skills, tools, index | `agentic_cli/dev_init.py · run_dev_init`, `agentic_cli/readiness.py · prerequisites` | ✅ M1-53 M1-33 |
| 3 | Start | Workspace: repo, policy repo, team, person; gateway credential | `agentic_cli/workspace.py · Workspace`, `agentic_cli/runtime.py · Runtime.gateway` | ✅ M1-54 M1-48 |
| 4 | Start | Org policy and definitions across org / team / repo (D33) | `agentic/policy/load.py · load_policy`, `agentic/definitions/discovery.py · DefinitionSet.discover` | ✅ M1-03 M1-04 |
| 5 | Preflight | Resolve agents, compatibility, conditions, skills, backends, sandbox | `agentic/engine/preflight.py · preflight`, `agentic/policy/resolve.py · resolve_agent`, `agentic/sandbox/readiness.py · decide_sandbox` | ✅ M1-25 |
| 6 | Preflight | RBAC, kill switch, CLI version, concurrency, team budget, pipeline integrity | control plane | 🔲 M2-21 M2-03 M2-08 E2-09 |
| 7 | Start | Run folder, run.json, run ledger | `agentic/engine/engine.py · Engine.start`, `sdlc_commons/ledger.py · RunLedger.open` | ✅ M1-21 M1-06 |
| 8 | Every step | `when`, kill switch | `agentic/engine/expressions.py · compile_condition` | ✅ M1-23 · 🔲 M2-19 |
| 9 | Every step | Worktree checkpoint before and after, commit trailers | `sdlc_commons/worktrees.py · StepCheckpoints`, `agentic/telemetry/trailers.py · commit_message` | ✅ M1-30 M1-45 |
| 10 | Every step | Tool context: guard, sandbox, skills, codeintel | `agentic_cli/wiring.py · StepWiring.tools_for`, `sdlc_commons/guards.py · Guard.for_agent`, `agentic/sandbox/srt.py · SrtSandbox` | ✅ M1-31 M1-32 M1-38 M1-36 |
| 11 | Every step | Agent loop on the native backend: tools, children, stuck check, limits, approval pauses | `agentic/backends/native.py · NativeBackend.runner`, `agentic/agents/runner.py · PydanticAIRunner`, `agentic/agents/children.py · ChildSpawner` | ✅ M1-15…M1-18 M1-28 |
| 12 | Every step | Agent-CLI backend (claude-code): built, not wired into the CLI | `agentic/backends/claude_code.py` | ✅ M1-29 · 🔲 DF-74 |
| 13 | Every model call | Secret scan, identity headers, corporate network, metering and budget, cost capture, span | `agentic/transport/scan.py`, `agentic/transport/client.py · make_async_client`, `agentic/agents/metering.py · MeteringTransport`, `agentic/transport/cost.py · cost_from_response`, `agentic/telemetry/spans.py` | ✅ M1-11…M1-14 M1-41 M1-43 M1-65 |
| 14 | Every tool result | Host paths scrubbed, secrets masked, fenced as untrusted | `agentic/agents/tools/protect.py · protect_tools`, `agentic/security/untrusted.py · untrusted` | ✅ M1-41 M1-42 |
| 15 | Every step end | Answer validated, retried once, handoff pinned, step_finished with cost | `agentic/handoffs/contract.py · HandoffContract`, `agentic/engine/engine.py · _Run._outcome` | ✅ M1-19 M1-20 M1-79 |
| 16 | Every step | Structured step log, redacted; `agentic logs [--follow]` | `agentic/telemetry/logs.py · StepLogs, LogTail`, `agentic_cli/commands/runs.py · logs` | ✅ M1-47 |
| 17 | Every step end | Events to the control plane | `agentic/telemetry/sinks.py · ForwardingRecorder` (no sink yet) | 🔲 M2-20 |
| 18 | Gates | Pause, show, approve / reject / edit, four-eyes, digest-pinned approval | `agentic/engine/engine.py · _Run.decide`, `agentic_cli/commands/gates.py`, `sdlc_commons/gates.py · ZyrofloGates` | ✅ M1-22 M1-55 · 🔲 M2-03 M2-07 |
| 19 | Loop | `until`, max iterations, oscillation, exhausted → gate | `agentic/engine/engine.py · _Run._loop, _Run._oscillating` | ✅ M1-24 |
| 20 | Review | Diff budget, deterministic pre-pass | review / security | 🔲 M2-24 M2-48 |
| 21 | End | Open the PR | GitHub adapter (`agentic/telemetry/trailers.py · pr_body` ready) | 🔲 M1-58 |
| 22 | End | run_closed, cost by step / agent / child | `agentic/engine/engine.py · _Run._close`, `agentic_cli/commands/cost.py` | ✅ M1-57 |
| 23 | Background | Heartbeat, kill-switch polling, budget alerts, auto-trip | control plane + CLI | 🔲 M3-07 M2-19 M2-16 M2-11 |

**Built:** everything a standalone run needs up to the pull request. That covers setup, preflight, the engine with gates and loops, the native backend with tools, children, sandbox, secret scan and skills, code intelligence, telemetry, and the ledgers.

**Not built yet:**
- opening the PR (M1-58), so the library pipelines' `publish` step fails preflight with `unknown action 'github.open_pr'` until it lands;
- wiring the claude-code backend into the CLI (DF-74);
- everything in the control plane (M2).
