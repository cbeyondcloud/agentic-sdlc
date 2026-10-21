"""The two-week release-age rule holds for every locked package, transitive ones
included (DF-95): uv refuses any release newer than the span when it resolves."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIN_DAYS = 14


def _days(span: str) -> int:
    """Days in a uv span: a friendly one ('14 days', '2 weeks') or ISO 8601 ('P14D')."""
    text = span.strip().lower()
    if found := re.fullmatch(r"p(?:(\d+)w)?(?:(\d+)d)?", text):
        return int(found[1] or 0) * 7 + int(found[2] or 0)
    found = re.fullmatch(r"(\d+)\s*(day|days|week|weeks)", text)
    assert found, f"not a relative span: {span!r}"
    return int(found[1]) * (7 if found[2].startswith("week") else 1)


def test_uv_refuses_releases_younger_than_two_weeks():
    settings = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    span = settings.get("tool", {}).get("uv", {}).get("exclude-newer")
    assert span, "[tool.uv] exclude-newer is not set"
    assert _days(span) >= MIN_DAYS


def test_the_lock_was_resolved_under_that_rule():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    span = lock.get("options", {}).get("exclude-newer-span")
    assert span, "uv.lock was not resolved with a relative exclude-newer"
    assert _days(span) >= MIN_DAYS
