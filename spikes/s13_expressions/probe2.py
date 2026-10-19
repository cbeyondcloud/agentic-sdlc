"""Spike S13 follow-up (throwaway): import cost on a warm run, `has()` for optional fields,
and null comparisons, in both CEL implementations."""

import time

CONTEXT = {"review": {"open_blocking": 0, "score": None}, "requirements": {"size": "small"}}
EXPRESSIONS = [
    "has(review.score)",
    "has(review.missing)",
    "review.score == null",
    "has(review.score) && review.score != null && review.score >= 8",
    "requirements.size != 'trivial' && !has(requirements.missing)",
]

started = time.perf_counter()
import cel  # noqa: E402

print(f"common-expression-language import {time.perf_counter() - started:.3f}s")
for e in EXPRESSIONS:
    try:
        print(f"  {e!r:70} -> {cel.evaluate(e, CONTEXT)!r}")
    except Exception as error:
        print(f"  {e!r:70} -> {type(error).__name__}: {str(error)[:60]}")

started = time.perf_counter()
import celpy  # noqa: E402

print(f"cel-python import {time.perf_counter() - started:.3f}s")
env = celpy.Environment()
for e in EXPRESSIONS:
    try:
        result = env.program(env.compile(e)).evaluate(celpy.json_to_cel(CONTEXT))
        print(f"  {e!r:70} -> {result!r}")
    except Exception as error:
        print(f"  {e!r:70} -> {type(error).__name__}: {str(error)[:60]}")
