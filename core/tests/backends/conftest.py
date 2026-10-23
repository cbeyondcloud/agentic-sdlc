from pathlib import Path

import pytest

from agentic.definitions import load_agent
from agentic.policy import OrgPolicy, ResolveContext, ResolvedAgent, load_policy, resolve_agent

_RESULTS = pytest.StashKey[list]()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "conformance(backend, tier): a runner conformance case (M2-41)"
    )
    config.stash[_RESULTS] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Record each conformance case's outcome by backend and tier, for the summary."""
    outcome = yield
    marker = item.get_closest_marker("conformance")
    report = outcome.get_result()
    if marker is None or (report.when != "call" and not (report.failed or report.skipped)):
        return
    from conformance import FAIL, NOT_APPLICABLE, PASS, SKIPPED

    if report.skipped:
        reason = str(report.longrepr[-1]) if isinstance(report.longrepr, tuple) else ""
        verdict = NOT_APPLICABLE if f"{NOT_APPLICABLE}:" in reason else SKIPPED
    else:
        verdict = FAIL if report.failed else PASS
    item.config.stash[_RESULTS].append((*marker.args, verdict))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """After a run that collected conformance cases: the backend x tier table."""
    results = config.stash.get(_RESULTS, [])
    if not results:
        return
    from conformance import summary_lines, verdicts

    from sdlc_commons.conformance import MEANING, SCRIPTED

    terminalreporter.section("runner conformance")
    for line in summary_lines(verdicts(results), SCRIPTED, MEANING[SCRIPTED]):
        terminalreporter.write_line(line)


@pytest.fixture
def policy(fixtures: Path) -> OrgPolicy:
    return load_policy(fixtures / "policy" / "policy")


@pytest.fixture
def resolved(fixtures: Path, policy: OrgPolicy) -> ResolvedAgent:
    """The fixture Developer agent, resolved for the payments team."""
    agent, violations = resolve_agent(
        load_agent(fixtures / "agents" / "developer.md"), policy, ResolveContext(team="payments")
    )
    assert agent is not None, violations
    return agent
