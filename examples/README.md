# Examples

## `sample-repo/`: the training repository

`invoices`, a tiny Python library (amounts in whole cents) with a passing test suite. It is
the one sample repository of agentic:

- the **acceptance scenarios** (`cli/tests/acceptance/`) copy it into a temporary git
  repository and run every pipeline on it;
- the **training** uses it: the platform team publishes it as an internal repository
  (`docs/runbooks/day-0-bootstrap.md`, "Training environment"), and developers follow the
  [15-minute walkthrough](../docs/training/walkthrough.md) on it: `agentic login`, `init`,
  `pipelines list`, `run --dry-run`, then a real run on recorded answers
  (`agentic training-gateway`, no model spend), `status`, `approve` and `stop`.

Change it with care: the acceptance tests check exact file contents (its `README.md`, its
tests), and the walkthrough's recorded answers were made on it. After a change, run
`uv run pytest cli/tests/acceptance` and, if a scenario's answers no longer fit,
re-record with `uv run python scripts/record_acceptance.py`.
