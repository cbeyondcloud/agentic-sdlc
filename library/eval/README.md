# Agent evaluation assets (M1-59)

Everything here is **synthetic** (`synthetic: true`, CC0-1.0) or permissively licensed
public material. Cases harvested from pilot or internal repositories are the adopter's
data and live in the policy repo's `eval/` folder or a repo's `.agentic/eval/`, never
here (decision 17). `load_corpus(..., public=True)` enforces this.

| Folder | What |
|---|---|
| `golden/<kind>/` | Three golden handoffs per kind (requirements, design, dev-report, review, test-report). Every one parses; they are rubric references and the ideal answers of the seed cases. |
| `rubrics/` | The judge rubrics (`ba-requirements`, `design`): 5 criteria x 0-2 = 10, versioned. Changing an anchor means a new version and a new human calibration (`agentic eval calibrate`). |
| `cases/<class>/` | One YAML file per case: `scoping` (BA and Design, rubric-judged), `review` (diffs with seeded defects), `qa` (seeded failing criteria), `dev_task` (fail-to-pass tests). Format: `agentic.eval.cases`. |
| `workspaces/` | Starting files of each `dev_task` (a Python package with its tests). |
| `solutions/` | Each `dev_task`'s reference change, laid over its workspace. |
| `manifest.json` | sha256 of every file above (LF-normalised); reports and A/B plans pin it. Rewrite it with `uv run python scripts/write_eval_manifest.py` after reviewing a change (it validates the corpus first). |
| `fixtures/` | CI fixture mode: a fixture policy (with an `eval-judge` tier), stand-in agent definitions, recorded model responses (`cassettes/`) and the expected reports. Not part of the manifest. |

`workspaces/` and `solutions/` hold code with deliberate bugs: they are data, excluded from
ruff, and never reformatted (a reformat would change the manifest).

## Size

The corpus meets the decision-18 floors on its own:

| Class | Holdout | Truth in holdout | Dev |
|---|---|---|---|
| scoping | 40 (20 BA, 20 Design) | rubric-judged | 6 |
| review | 20 diffs | 45 seeded defects | 5 |
| dev_task | 20 tasks | 31 fail-to-pass tests | 5 |
| qa | 15 cases | 30 seeded failing criteria | 5 |

Review diffs cover Python, TypeScript, Go, Java, C#, Rust, Kotlin, SQL, PowerShell, bash
and CI YAML; cases span health, logistics, HR, IoT, education, retail, finance, privacy,
support, mobility and developer tooling, with Windows and POSIX concerns (separators,
CRLF, case-insensitive file systems, file locking, shell quoting, home folders, encodings)
throughout. Dev tasks are Python with pytest, so they run on every laptop and runner with
no extra toolchain. `agentic.eval.corpus.floor_shortfalls` reports any gap.

## How cases are checked

`load_corpus` refuses a case whose truth cannot be true (`agentic.eval.validate`): an input
handoff without its kind's fields; a review diff whose hunk counts do not add up, or a
seeded defect outside the diff's hunks; a QA failing criterion the requirements lack, or
a QA case with no criterion left passing; a dev task without its reference solution or
naming a test outside its workspace. `core/tests/eval/test_eval_devtask.py` runs every dev
task: its fail-to-pass tests must fail on the workspace and pass with the solution.

To add a case: write the YAML (and, for a dev task, the workspace, tests and solution),
run `uv run python scripts/write_eval_manifest.py`, then the eval tests. Seeded-defect
line numbers are new-file lines inside one hunk.

## Fixture mode

CI replays only the cases tagged `fixture` (the cassette subset, about one in seven
cases); the rest of the corpus is for live runs.

`agentic eval run --agent <name> --corpus library/eval --mode replay --tag fixture`
replays the recorded responses in `fixtures/cassettes/` (no model is called) and writes a
report; the tests compare it with `fixtures/expected/<agent>.json`. After changing a
prompt, an agent stand-in or a fixture case, re-record with
`uv run python scripts/record_eval_fixtures.py` (a scripted upstream, still no model) and
review the diff of the expected reports.

A live run uses the whole corpus: `agentic eval run --agent <name> --corpus library/eval
--mode live --gateway <TokenLean URL>` (holdout split by default).
