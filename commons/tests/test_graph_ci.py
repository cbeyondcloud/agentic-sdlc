"""Where CI demands it, a missing Zyroflo installer fails the domain-graph tests instead
of skipping them, so CI cannot stay green while testing none of the impact paths (DF-463)."""

import pytest
from graph_ci import REQUIRE_GRAPH, skip_unless_required


def test_missing_installer_skips_outside_ci():
    with pytest.raises(pytest.skip.Exception, match="no installer"):
        skip_unless_required("no installer", environ={})


def test_missing_installer_fails_where_ci_requires_the_graph():
    with pytest.raises(pytest.fail.Exception, match="no installer"):
        skip_unless_required("no installer", environ={REQUIRE_GRAPH: "1"})
