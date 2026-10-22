"""Where the test database comes from: AGENTIC_CP_TEST_DSN, else a container started through
CONTAINER_ENGINE (docker or podman). Without either, database tests skip with a reason
that names both - or fail, when AGENTIC_CP_REQUIRE_PG=1 (the Linux check stage, CI)."""

import pytest
from pg import Source, find_source, unavailable


def _which(*present):
    return lambda name: f"/bin/{name}" if name in present else None


def test_a_test_dsn_wins_over_an_engine():
    found = find_source({"AGENTIC_CP_TEST_DSN": "postgresql://x@h/db"}, _which("docker"))
    assert found == Source(dsn="postgresql://x@h/db", engine=None)


def test_an_engine_on_path_is_used():
    assert find_source({}, _which("podman")) == Source(dsn=None, engine="podman")
    assert find_source({"CONTAINER_ENGINE": "podman"}, _which("docker", "podman")).engine == (
        "podman"
    )


def test_missing_database_skips_with_reason():
    reason = find_source({}, _which())
    assert isinstance(reason, str)
    with pytest.raises(pytest.skip.Exception) as caught:
        unavailable(reason, {})
    assert "CONTAINER_ENGINE" in str(caught.value)
    assert "AGENTIC_CP_TEST_DSN" in str(caught.value)


def test_missing_database_fails_when_required():
    reason = find_source({"AGENTIC_CP_REQUIRE_PG": "1"}, _which())
    with pytest.raises(pytest.fail.Exception) as caught:
        unavailable(reason, {"AGENTIC_CP_REQUIRE_PG": "1"})
    assert "AGENTIC_CP_TEST_DSN" in str(caught.value)


def test_a_named_engine_that_is_missing_is_a_reason_not_a_crash():
    reason = find_source({"CONTAINER_ENGINE": "podman"}, _which("docker"))
    assert isinstance(reason, str) and "podman" in reason
