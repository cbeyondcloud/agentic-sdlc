"""CI verdict adapter over Zyroflo's release flow (M1-58; D34: reuse, never rewrite).

Zyroflo's `zyroflo.sdlc.release` is the P8 flow the framework follows: push through the
wall, open the pull request, watch the real CI - and never click merge. Its `gh` calls
are not used (the framework talks to the hosting API itself, so `gh` is never needed);
its judgement is, unchanged:

  * `_rest_bucket` - a REST check row's `conclusion` (or, while it runs, `status`) in
    the verdict's own vocabulary, translated at the boundary;
  * `verdict` - five answers, and only one is a pass: green; red (a check failed);
    pending (still running); none (NO checks: "nothing ran" is not "nothing failed");
    unknown (a word nobody knows, never read as a pass).

Rows are plain mappings (`name`, `status`, `conclusion`, `link`) - what
`agentic.hosting.CheckRun` holds - so this package needs nothing from core.
"""

from collections.abc import Iterable, Mapping

from zyroflo.sdlc import release as zyroflo

GREEN, RED, PENDING, NONE, UNKNOWN = (
    zyroflo.GREEN,
    zyroflo.RED,
    zyroflo.PENDING,
    zyroflo.NONE,
    zyroflo.UNKNOWN,
)


def ci_verdict(rows: Iterable[Mapping[str, object]]) -> dict:
    """What the checks add up to: Zyroflo's verdict (`state`, `checks`, `failed`,
    `pending`, `unknown`, `why`), plus Zyroflo's `pending` split in two - `running` (no
    conclusion yet) and `skipped` (finished skipped or neutral: not a pass, but nothing
    to wait for, so a watch settles on it and `why` says "skipped", not "running")."""
    translated, running, skipped = [], [], []
    for row in rows:
        # `_rest_bucket` reads Zyroflo's `gh --jq` shape: `bucket` = conclusion, `state` = status
        rest = {"bucket": row.get("conclusion"), "state": row.get("status")}
        name = str(row.get("name") or "?")
        bucket = zyroflo._rest_bucket(rest)
        if zyroflo.BUCKETS.get(bucket) == PENDING:
            (running if bucket == "pending" else skipped).append(name)
        translated.append({"name": name, "bucket": bucket, "link": str(row.get("link") or "")})
    found = zyroflo.verdict(translated)
    if found["state"] == PENDING and skipped:
        said = f"{len(skipped)} check(s) skipped or neutral, which is not a pass: "
        said += ", ".join(skipped)
        found["why"] = f"{len(running)} check(s) still running; {said}" if running else said
    return {**found, "running": running, "skipped": skipped}
