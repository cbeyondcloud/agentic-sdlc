---
# =============================================================================
# AGENT DEFINITION TEMPLATE
# Copy to .agentic/agents/<your-agent-name>.md and edit.
# Only name, version and description are required.
# Optional settings are shown commented out with a sample value; to use one,
# delete the leading "# ". The value in [brackets] is what applies if you leave
# the setting out. Settings marked (policy) are requests only: org and team
# policy decide the final value, and a forbidden request stops the run with an
# explanation before anything is spent.
# =============================================================================

name: my-agent                         # required; lowercase-with-hyphens, unique in the repo
version: 0.1.0                         # required; MAJOR.MINOR.PATCH
description: One line saying what this agent does.   # required

# --- identity ----------------------------------------------------------------
# owner: payments-team                 # team or person responsible   [unset]
# status: active                       # active | deprecated   [active]

# --- model (policy) ----------------------------------------------------------
# tier: balanced                       # an org tier, e.g. deep-reasoning | balanced | fast   [balanced]
# model: anthropic/claude-sonnet       # exact model; normally leave unset and use tier   [unset]
# effort: medium                       # low | medium | high   [unset = model default]
# temperature: 0.2                     # 0.0 to 2.0   [unset = model default]

# --- where it runs and what it may use (policy) -------------------------------
# backend: native                      # native | claude-code | codex | opencode | acp:<agent> | remote:<service>   [native]
# tools: [read_files, search, edit_files, run_shell, codeintel, spawn_agent]   # allow-list: anything not listed is unavailable   [none]
# shell: [pytest, "npm test"]          # command prefixes it may run (needs run_shell)   [none]
# skills: [superpowers/test-driven-development]   # names from the org skill catalog   [none]

# --- handoffs: documents it reads and writes ----------------------------------
# consumes: [design]                   # kinds: requirements | design | dev-report | review | test-report   [none]
# produces: dev-report                 # the one kind it writes   [unset]

# --- limits per run (policy clamps each to the org/team maximum) --------------
# limits:
#   budget_usd: 5                      # US dollars   [policy default]
#   max_turns: 80                      # model round-trips   [policy default]
#   max_minutes: 60                    # wall-clock minutes   [policy default]

# --- child agents it may spawn (policy) ----------------------------------------
# children:
#   max_total: 3                       # 0 = may not spawn children   [0]
#   max_concurrent: 2                  # running at the same time   [1]
#   max_depth: 1                       # 2 = children may spawn their own children   [1]
#   per_child: { budget_usd: 1, max_turns: 30, max_minutes: 15 }   # each child's share   [policy default]

# --- guardrails: what this agent may NEVER do, even if policy allows it --------
# deny:
#   shell: ["git push", "git commit --amend"]   # command prefixes, always refused
#   paths: [".github/**", "infra/**"]           # files it may never write
---
Describe the agent's job here in plain English. This text is the agent's
instructions; it is sent to the model at the start of every run.

A good description says:
- who the agent is ("You are the Code Review agent for the payments service")
- what it receives (the handoff documents listed in `consumes`)
- what it must produce (the handoff listed in `produces`) and how good it must be
- the team conventions it must follow
- what to do when unsure (ask at the next human gate instead of guessing)
