# Auditor guide

For security and compliance. You verify that the record of what agents did is complete
and unchanged, export it with proof for an audit, review policy and skill changes, and
review retention. Every command is in the [command reference](command-reference.md).

You hold the `auditor` role. It can read the org's audit chain and traces, export audit
records and view org dashboards, including cost. It cannot run pipelines or approve gates.
To sign in, follow steps 1 to 3 of the [developer guide](developer.md).

## What is recorded

| Record | Where | Holds |
|---|---|---|
| Run ledger | each run's folder on the laptop, and its events at the control plane | every step, gate decision, tool approval, skill activation (name, version, hash), cost and the policy version the run used |
| Org audit chain | the control plane, per tenant | role changes, invites, offboarding, kill switches, policy versions accepted or rejected, routing applies, personal backups (path and hash, never content), learning decisions, exports, retention runs |
| Gateway audit | the gateway (TokenLean) | each model call, its team and its tags |

Both chains are hash chains: each record holds the hash of the one before. Records are
insert-only in the database; the runtime role cannot change or delete them.

## Verify

```bash
agentic audit verify                     # every run ledger in this repository
agentic audit verify <run>               # one run
agentic audit verify --control-plane     # also the org's audit chain
```

Exit 0 means intact. Exit 3 means a chain does not verify; each problem names where the
change was made. The platform team can also verify one tenant's chain on the server with
`python -m agentic_cp admin audit verify --tenant <slug>`.

## Export for an audit

```bash
agentic audit export --from 2026-07-01 --to 2026-10-01 --format jsonl
agentic audit export --team payments --from 2026-10-01 --to 2026-11-01 --format csv -o payments-oct.csv
```

`--from` is inclusive, `--to` exclusive. Records of other teams in a team export are
`link` rows: sequence and hashes only. Both formats carry the chain proof: a header row
(the anchor before the range) and a proof row (counts, end anchor, chain head). A file
with rows removed at either end does not verify. The command verifies the file after
writing it.

**Verify an export without our software.** Give the external auditor the file and
`core/src/agentic/auditproof.py` (one file, Python standard library only):

```bash
python auditproof.py audit-org-2026-07-01-2026-10-01.jsonl     # exit 0 intact, 3 broken
```

Or with the CLI: `agentic audit verify --export <file>`.

**Keep each export's end anchor.** The next export's start must continue it, which proves
that nothing was removed between two exports.

In CSV, a cell a spreadsheet would run as a formula starts with one `'`. The verifier
strips it.

## Quarterly review

| Review | Where |
|---|---|
| Policy changes | pull requests to the org policy repo. The template's CODEOWNERS names a security group (`@your-org/agentic-security`) next to the org admins on `policy/`, `skills/` and `CODEOWNERS`, so you are asked to review those changes. GitHub needs only one code owner's approval: to make yours required, ask the org admins for a branch ruleset that requires a review from your team on those paths. See the [policy-repo guide](policy-repo.md) |
| Skills with scripts, new skills in the org catalog | policy repo `skills/` pull requests |
| Retention periods and legal holds | [retention runbook](../runbooks/retention.md); periods are in `policy/retention.yaml` |
| Who holds which role | ask an org admin for `agentic rbac assignments`; every change is in the audit chain |
| Kill switches set and cleared | the audit chain (`killswitch.set`, `killswitch.cleared`) |
| Cost by team | `agentic report chargeback --month YYYY-MM`, or the Executive ROI dashboard |

## What never leaves a laptop

Before any content goes to a model or the control plane, a secret scan runs on it.
Personal skill content is backed up only for its owner; the audit chain holds its path and
hash, never its content. A support bundle never holds a ledger, handoff, prompt, diff or
source file (see [CLI operations](../runbooks/cli-operations.md)).

See also: [call flow](../architecture/call-flow.md) for every step a run takes and what
it records.
