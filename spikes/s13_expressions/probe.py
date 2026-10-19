"""Spike S13 (throwaway, never imported): which safe evaluator for pipeline `when` / `until`?

Run in an ephemeral environment, so the project's lock file is untouched:
    uv run --no-project --python 3.12 --with simpleeval==1.0.8 \
        --with common-expression-language==0.10.0 --with cel-python==0.5.0 \
        python spikes/s13_expressions/probe.py

Inputs are the handoff front-matter fields of earlier steps, by kind:
    {"requirements": {...}, "review": {...}}
"""

import time
import traceback

CONTEXT = {
    "requirements": {"size": "small", "items": [{"id": "REQ-1"}], "open_questions": []},
    "review": {"open_blocking": 0, "score": 8.5, "findings": [{"severity": "low"}]},
}

# (label, expression, expected) - expected None means "must be refused or error"
CASES = [
    ("spec when", "requirements.size != 'trivial'", True),
    ("spec until", "review.open_blocking == 0", True),
    ("and", "review.open_blocking == 0 && review.score >= 8", True),
    ("in list", "requirements.size in ['small', 'medium']", True),
    ("list size", "size(review.findings) == 1", True),
    ("missing field", "review.missing == 0", None),
    ("missing kind", "design.decisions == 0", None),
    ("dunder attr", "review.__class__", None),
    ("python escape", "().__class__.__bases__[0].__subclasses__()", None),
    ("import", "__import__('os').system('echo pwned')", None),
    ("power bomb", "9 ** 9 ** 9", None),
    ("string bomb", "'a' * 1000000000", None),
]

PY_STYLE = {"&&": "and", "size(review.findings)": "len(review.findings)"}


def to_python(expression: str) -> str:
    for cel, py in PY_STYLE.items():
        expression = expression.replace(cel, py)
    return expression


def run_simpleeval():
    from simpleeval import EvalWithCompoundTypes

    class Dot(dict):  # field access as attributes, over plain dicts
        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            value = self[name]
            return Dot(value) if isinstance(value, dict) else value

    names = {k: Dot(v) for k, v in CONTEXT.items()}

    def evaluate(expression):
        return EvalWithCompoundTypes(names=names, functions={"len": len}).eval(
            to_python(expression)
        )

    return evaluate


def run_cel_rust():
    import cel  # common-expression-language (Rust core)

    def evaluate(expression):
        return cel.evaluate(expression, CONTEXT)

    return evaluate


def run_cel_python():
    import celpy

    env = celpy.Environment()

    def evaluate(expression):
        program = env.program(env.compile(expression))
        return program.evaluate(celpy.json_to_cel(CONTEXT))

    return evaluate


def probe(name, factory):
    print(f"\n=== {name}")
    try:
        started = time.perf_counter()
        evaluate = factory()
        print(f"  import+setup {time.perf_counter() - started:.3f}s")
    except Exception:
        print("  NOT AVAILABLE:", traceback.format_exc().splitlines()[-1])
        return
    for label, expression, expected in CASES:
        started = time.perf_counter()
        try:
            result = evaluate(expression)
            outcome = repr(result)[:60]
            ok = (expected is not None) and bool(result) == expected
            verdict = "ok" if ok else ("WRONG: evaluated" if expected is None else "WRONG")
        except Exception as error:
            outcome = f"{type(error).__name__}: {str(error)[:70]}"
            verdict = "ok (refused)" if expected is None else "WRONG: raised"
        took = time.perf_counter() - started
        print(f"  {label:14} {verdict:18} {took * 1000:7.1f}ms  {outcome}")
    started = time.perf_counter()
    for _ in range(1000):
        evaluate("review.open_blocking == 0")
    print(f"  1000 evals of the spec until: {time.perf_counter() - started:.3f}s")


if __name__ == "__main__":
    probe("simpleeval 1.0.8 (MIT)", run_simpleeval)
    probe("common-expression-language 0.10.0 (Apache-2.0, Rust)", run_cel_rust)
    probe("cel-python 0.5.0 (Apache-2.0)", run_cel_python)
