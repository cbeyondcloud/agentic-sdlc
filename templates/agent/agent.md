---
# =============================================================================
# AGENT DEFINITION TEMPLATE
# Copy to .agentic/agents/<your-agent-name>.md and edit.
# Required: name, type, version and description.
# Every agent has an org-defined TYPE (e.g. developer, code-review, general).
# The type is the org-approved template: any setting you leave out is
# inherited from it, and any setting you write may only NARROW it (fewer
# tools, a smaller budget...). Caps come from the type, never from the name.
# Optional settings are shown commented out with a sample value; to use one,
# delete the leading "# ". [brackets] say what applies if you leave it out.
# A request beyond the type or org policy stops the run with an explanation
# before anything is spent.
# =============================================================================

name: my-agent                         # required; lowercase-with-hyphens, unique in the repo
type: general                          # required; an agent type defined by your org (agentic types list)
version: 0.1.0                         # required; MAJOR.MINOR.PATCH
description: One line saying what this agent does.   # required

# --- identity ----------------------------------------------------------------
# owner: payments-team                 # team or person responsible   [unset]
# status: active                       # active | deprecated   [active]

# --- model ---------------------------------------------------------------------
# tier: balanced                       # an org tier, e.g. deep-reasoning | balanced | fast   [the type's tier]
# model: anthropic/claude-sonnet       # exact model; only org-approved models; normally leave unset   [from the tier]
# effort: medium                       # low | medium | high   [the tier's default]
# temperature: 0.2                     # 0.0 to 2.0   [the tier's default]

# --- where it runs and what it may use (within the type's ceiling) ---------------
# backend: native                      # native | claude-code | codex | opencode | acp:<agent> | remote:<service>   [the type's first backend]
# tools: [read_files, search, edit_files]   # allow-list; anything not listed is unavailable   [the type's tools]
# shell: [pytest, "npm test"]          # command prefixes it may run (needs run_shell)   [the type's shell list]
# skills: [superpowers/test-driven-development]   # names from the org skill catalog   [the type's skills]

# --- handoffs: documents it reads and writes ----------------------------------
# consumes: [design]                   # kinds: requirements | design | dev-report | review | test-report   [the type's list]
# produces: dev-report                 # must match the type's kind if the type fixes one   [the type's kind]

# --- limits per run (never above the type's caps; a team may lower them) --------
# limits:
#   budget_usd: 1                      # US dollars   [the type's cap]
#   max_turns: 30                      # model round-trips   [the type's cap]
#   max_minutes: 20                    # wall-clock minutes   [the type's cap]

# --- child agents it may spawn (never above the type's caps) ---------------------
# children:
#   max_total: 0                       # 0 = may not spawn children   [the type's cap]
#   max_concurrent: 0                  # running at the same time   [the type's cap]
#   max_depth: 0                       # 1 = children may not spawn their own   [the type's cap]
#   per_child: { budget_usd: 0, max_turns: 0, max_minutes: 0 }   # each child's share   [the type's cap]

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
