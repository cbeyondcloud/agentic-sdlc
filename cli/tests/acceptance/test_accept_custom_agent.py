"""Acceptance 2 (spec §1.3, M1-69): a person defines a new agent - a "Docs Writer" - in
plain English and adds it to `feature-delivery` without writing code. The agent file
and one pipeline entry are the whole change.

What runs for real: `agentic agents new` (the file from the template), the person's
plain-English edit, `agentic validate`, `agentic agents list`, one entry added to the
person's own copy of the pipeline (standalone policy), `run --dry-run` resolving the
new agent from the repo layer against the `general` type, and the full run on the
sample repo with the Docs Writer's step after the build loop: it reads and edits the
README in the run's tree, its edit is a checkpoint on the run branch and reaches the
pull request. Stubbed as in M1-68: models (cassette), GitHub API, init's index build,
merging init's PR, and the OS sandbox on Windows native.
"""

import difflib
import re

from _acceptance_support import (
    Scenario,
    approvals,
    assert_published,
    git,
    trailers_by_step,
)
from _acceptance_support import (
    tool as tool_call,
)
from _invoice_story import REQUEST, SPLIT_FINAL, one_round_script

from agentic_cli.exit_codes import ExitCode

SCENARIO = "m1-69"
AGENT = ".agentic/agents/docs-writer.md"
DESCRIPTION = "Keeps the README in step with the change: an example for each new function."
INSTRUCTIONS = """\
You are the Docs Writer for this repository.

Read the approved requirements, the design and the developer's report, then the code
they changed. Update README.md so that a reader can use what was added: for every new
public function, add a short section with one runnable example and one sentence about
its edge cases. Change Markdown files only - never code or tests.

When you are done, reply with one short paragraph saying which sections you added or
changed, and why.
"""
ENTRY = """\
  - id: docs                            # the Docs Writer documents the change
    agent: docs-writer

"""
SECTION = """\
## Splitting a total

```python
from invoices.split import split_evenly

split_evenly(1000, 3)  # [334, 333, 333]
```

The first payers take the extra cents; fewer than one payer raises ValueError.

"""
DOCS_REPLY = "Added a 'Splitting a total' section to README.md with a split_evenly example."


def script() -> dict:
    turns = one_round_script()
    turns["docs"] = [
        tool_call("read_file", path="README.md"),
        tool_call("edit_file", path="README.md", old="## Tests\n", new=SECTION + "## Tests\n"),
        DOCS_REPLY,
    ]
    return turns


def write_docs_writer(scenario: Scenario, repo):
    """`agentic agents new`, then the person writes the job in plain English."""
    made = scenario.ok("--json", "agents", "new", "docs-writer", "--type", "general").json()
    assert made["problems"] == [], made
    path = repo / AGENT
    text = path.read_text(encoding="utf-8")
    head = text[: text.index("\n---\n", 4) + len("\n---\n")]
    plain = re.sub(r"(?m)^description: .*$", f"description: {DESCRIPTION}", head, count=1)
    path.write_text(plain + INSTRUCTIONS, encoding="utf-8", newline="\n")
    # the description has ': ' in it, as plain English does: validate says what to do
    slip = scenario.ok("--json", "validate", AGENT, code=ExitCode.INVALID).json()
    assert "put the value in quotes" in slip["problems"][0]["reason"], slip
    quoted = plain.replace(f"description: {DESCRIPTION}", f'description: "{DESCRIPTION}"')
    path.write_text(quoted + INSTRUCTIONS, encoding="utf-8", newline="\n")
    scenario.ok("--json", "validate", AGENT)
    git(repo, "add", AGENT)
    git(repo, "commit", "-qm", "Add a Docs Writer agent")


def add_pipeline_entry(scenario: Scenario) -> tuple[list[str], list[str]]:
    """One entry in the person's feature-delivery: after the build loop, before the last
    gate. Returns the file's lines before and after."""
    path = scenario.standalone / "pipelines" / "feature-delivery.yaml"
    before = path.read_text(encoding="utf-8")
    after = before.replace("  - gate: review-approval", ENTRY + "  - gate: review-approval", 1)
    path.write_text(after, encoding="utf-8", newline="\n")
    return before.splitlines(), after.splitlines()


def test_a_plain_english_agent_joins_the_pipeline_without_code(scenario: Scenario):
    repo = scenario.existing_repo()
    scenario.init_standalone(repo)
    set_up = git(repo, "rev-parse", "HEAD")

    write_docs_writer(scenario, repo)
    before, after = add_pipeline_entry(scenario)

    # the whole change: one agent file in the repo, one entry in the pipeline
    assert git(repo, "diff", "--name-only", set_up, "HEAD").splitlines() == [AGENT]
    diff = list(difflib.ndiff(before, after))
    assert [line[2:] for line in diff if line.startswith("- ")] == []
    assert [line[2:] for line in diff if line.startswith("+ ")] == ENTRY.splitlines()

    agents = scenario.ok("--json", "agents", "list").json()
    [row] = [a for a in agents if a["name"] == "docs-writer"]
    assert row["layer"] == "repo" and row["type"] == "general"

    main_head = git(repo, "rev-parse", "HEAD")
    scenario.models(script())
    plan = scenario.ok("--json", "run", "feature-delivery", "--dry-run").json()
    assert plan["preflight"]["passed"], plan["preflight"]
    [docs] = [s for s in plan["steps"] if s.get("id") == "docs"]
    assert (docs["agent"], docs["layer"], docs["type"]) == ("docs-writer", "repo", "general")
    assert docs["tools"] == ["read_files", "search", "edit_files"]  # the type's, inherited

    run = scenario.ok(
        "--json", "run", "feature-delivery", "--input", f"request={REQUEST}", code=ExitCode.PAUSED
    ).json()
    run_id = run["run_id"]
    for gate in ("requirements-approval", "design-approval"):
        assert run["pending"]["id"] == gate
        run = scenario.ok("--json", "approve", run_id, code=ExitCode.PAUSED).json()
    assert run["pending"]["id"] == "review-approval"
    done = scenario.ok("--json", "approve", run_id).json()
    assert done["status"] == "completed", done
    assert scenario.finish_models() == []

    steps = [h["step"] for h in done["history"]]
    assert steps == [
        "requirements",
        "baseline",
        "design",
        "develop",
        "review",
        "qa",
        "docs",  # the new agent, where the entry put it
        "publish",
    ]
    assert [a["verdict"] for a in approvals(repo, run_id)] == ["approve"] * 3

    branch = f"sdlc/{run_id}"
    assert list(trailers_by_step(scenario.bare, branch)) == ["docs #1", "develop #1"]
    readme = git(scenario.bare, "show", f"{branch}:README.md") + "\n"
    assert SECTION + "## Tests\n" in readme
    assert git(scenario.bare, "show", f"{branch}:invoices/split.py") + "\n" == SPLIT_FINAL
    shown = git(scenario.bare, "show", "--name-only", "--format=", branch).split()
    assert shown == ["README.md"]  # the Docs Writer changed Markdown only

    pull = assert_published(scenario, repo, run_id, main_head, title=REQUEST)
    assert f"- docs `.agentic/local/runs/{run_id}/handoffs/docs-1.md`" in pull["body"]
    reply = (repo / ".agentic" / "local" / "runs" / run_id / "handoffs" / "docs-1.md").read_text(
        encoding="utf-8"
    )
    assert DOCS_REPLY in reply
