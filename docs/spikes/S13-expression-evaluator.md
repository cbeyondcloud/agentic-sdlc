# S13 — Safe expression evaluator for `when` / `until`

**Question.** Which evaluator should pipelines use for step conditions (`when: "requirements.size != 'trivial'"`) and loop exits (`until: "review.open_blocking == 0"`)? It reads the front-matter fields of earlier handoffs, by kind. It must allow field access and comparisons, and nothing else: no attribute or import escape, no resource bombs (master plan M1-23).

**Method.** Throwaway probes in `spikes/s13_expressions/`, run in an ephemeral environment (`uv run --no-project --with ...`, so the project's lock file is untouched) on Windows, Python 3.12. Twelve cases ran against each candidate:
- the two spec expressions, `&&`, `in`, `size()`
- a missing field and a missing kind
- `__class__`, a `().__class__.__bases__` escape, `__import__`
- `9 ** 9 ** 9` and `'a' * 10**9`
- 1,000 evaluations for speed

A follow-up checked `has()` on optional fields, `null`, and import cost on a warm run.

| Candidate | Licence | Dependencies | Cases | 1,000 evals | Warm import |
|---|---|---|---|---|---|
| simpleeval 1.0.8 | MIT | none | 12/12 (Python syntax: `and`, `len`) | 0.03 s | ~0.02 s |
| common-expression-language 0.10.0 (Rust core) | Apache-2.0 | typer, rich, prompt-toolkit, pygments | 12/12, plus `has()`, `null` | 0.04 s | 0.24 s |
| cel-python 0.5.0 (Cloud Custodian) | Apache-2.0 | google-re2, pendulum, lark, pyyaml, jmespath | 12/12, plus `has()`, `null` | 0.25 s | 0.06 s |

**Result.** All three refused every escape and bomb, and all evaluated the spec's expressions correctly. The spec's examples are valid both as CEL and as Python syntax.

**Decision.** The language is **CEL** (Common Expression Language), through **cel-python 0.5.0**, wrapped behind one small `agentic` module so the implementation can be swapped.

Why CEL over simpleeval:
- CEL is designed for exactly this. It cannot loop, so it always terminates; it has no I/O and no host objects, and it is typed with no implicit coercion (`'a' * 3` is a type error, not a large string).
- It is an open standard (used by Kubernetes, Envoy and Google IAM), so the same expressions can be evaluated later by the control plane or another language.
- `has(review.score)` tests optional fields safely.
- simpleeval's safety rests on a blocklist over a Python expression interpreter, and attribute access on host objects is its known risk area.

Why cel-python over the Rust package:
- Compile and evaluate are separate steps, so preflight can syntax-check every `when`/`until` when a pipeline loads, before any spend.
- Errors are typed with positions (`CELParseError`, `CELEvalError`), which makes readable preflight messages.
- It is a library. The Rust package brings a CLI stack (typer, rich, prompt-toolkit) into the core library.
- Its speed (0.25 ms per evaluation) is irrelevant at a few evaluations per step.

**Rules for the engine (M1-23):**
- The context is `{kind: front-matter fields}` of the handoffs produced so far in the run.
- A missing field or kind is an error at evaluation time; conditions use `has()` for optional fields.
- The result must be a boolean, otherwise it is an error.
- Expressions are compiled at pipeline load (preflight) and evaluated with the run's context.

**Cost if wrong.** The wrapper module is the only place that imports `celpy`. Switching to the Rust package, or to simpleeval with a Python-syntax translation, touches one file and its tests.
