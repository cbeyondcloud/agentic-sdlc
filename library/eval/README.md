# Agent evaluation assets (M1-59)

Everything here is **synthetic** (`synthetic: true`, CC0-1.0) or permissively licensed
public material. Cases harvested from pilot or internal repositories are the adopter's
data and live in the policy repo's `eval/` folder or a repo's `.agentic/eval/`, never
here (decision 17). `load_corpus(..., public=True)` enforces this.

| Folder | What |
|---|---|
| `golden/<kind>/` | Three golden handoffs per kind (requirements, design, dev-report, review, test-report). Every one parses; they are rubric references and the ideal answers of the seed cases. |
| `rubrics/` | The judge rubrics (`ba-requirements`, `design`): 5 criteria x 0-2 = 10, versioned. Changing an anchor means a new version and a new human calibration (`agentic eval calibrate`). |
| `cases/<class>/` | The seed corpus, one YAML file per case: `scoping` (BA and Design, rubric-judged), `review` (seeded defects), `qa` (seeded failing criteria), `dev_task` (fail-to-pass tests). Format: `agentic.eval.cases`. |
| `workspaces/` | Starting files for `dev_task` cases. |
| `manifest.json` | sha256 of every file above (LF-normalised); reports and A/B plans pin it. Rewrite it with `agentic.eval.corpus.write_manifest` after reviewing a change. |
| `fixtures/` | CI fixture mode: a fixture policy (with an `eval-judge` tier), stand-in agent definitions, recorded model responses (`cassettes/`) and the expected reports. Not part of the manifest. |

## Size

The seed corpus is a start, not the M1-64 gate corpus. Decision 18 sets the floors the
gate needs: at least 20 scoping cases, 20 review diffs carrying 40 seeded defects, 20
developer tasks, 30 seeded QA failing criteria, and 5 dev-split cases per class.
`agentic.eval.corpus.floor_shortfalls` lists what is still missing; the rest comes from
the pilot-repo harvest and the seeded-defect tooling (deferred, see the SP18a plan).

## Fixture mode

`agentic eval run --agent <name> --corpus library/eval --mode replay` replays the
recorded responses in `fixtures/cassettes/` (no model is called) and writes a report;
the tests compare it with `fixtures/expected/<agent>.json`. After changing a prompt,
an agent stand-in or a case, re-record with `uv run python scripts/record_eval_fixtures.py`
(a scripted upstream, still no model) and review the diff of the expected reports.
