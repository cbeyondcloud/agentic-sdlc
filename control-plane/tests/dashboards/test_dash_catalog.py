"""The Grafana dashboards (M2-38; M3-01 executive ROI): rendered from
`dashboards.catalog`, committed as JSON, reading only the `dash_*` views, with no vendor
names in titles."""

import json
import re

from agentic_cp.__main__ import build_parser
from agentic_cp.dashboards.catalog import (
    DASHBOARDS,
    DATASOURCE,
    GRAFANA_DIR,
    GRAFANA_DIRS,
    GRAFANA_EXECUTIVE_DIR,
    MACROS,
    directory_of,
    render,
    rendered_files,
)
from agentic_cp.dashboards.views import DASH_VIEWS

VENDORS = (
    "anthropic",
    "claude",
    "openai",
    "gpt",
    "codex",
    "gemini",
    "google",
    "mistral",
    "cohere",
    "llama",
    "copilot",
    "opencode",
    "tokenlean",
    "langfuse",
    "grafana",
)
EXPECTED = {
    "agentic-org",
    "agentic-team",
    "agentic-run-explorer",
    "agentic-agent-quality",
    "agentic-limits-safety",
    "agentic-audit-explorer",
    "agentic-adoption",
    "agentic-executive-roi",
}


def test_eight_dashboards_with_unique_uids():
    assert {d.uid for d in DASHBOARDS} == EXPECTED
    assert len({d.title for d in DASHBOARDS}) == 8
    for dashboard in DASHBOARDS:
        panels = render(dashboard)["panels"]
        assert panels, dashboard.uid
        assert len({p["id"] for p in panels}) == len(panels)


def test_no_vendor_names_in_titles():
    for dashboard in DASHBOARDS:
        rendered = render(dashboard)
        titles = [rendered["title"], *(p["title"] for p in rendered["panels"])]
        titles += [v["label"] for v in rendered["templating"]["list"]]
        for title in titles:
            assert not any(vendor in title.lower() for vendor in VENDORS), title


def test_committed_json_is_current():
    every: set[str] = set()
    for directory in GRAFANA_DIRS:
        files = rendered_files(directory)
        every |= set(files)
        committed = {p.name for p in directory.glob("*.json")}
        assert committed == set(files), "run `uv run python -m agentic_cp dashboards --write`"
        for name, text in files.items():
            on_disk = (directory / name).read_bytes().decode("utf-8").replace("\r\n", "\n")
            assert on_disk == text, (
                f"{name} is stale: run `python -m agentic_cp dashboards --write`"
            )
            json.loads(text)
    assert every == {f"{d.uid.removeprefix('agentic-')}.json" for d in DASHBOARDS}


def test_per_person_cost_has_its_own_directory():
    """Review F3: Executive ROI shows each person's cost, so its JSON lives apart from the
    other dashboards and is provisioned into its own Grafana folder, which an operator limits
    to the org viewers; the shared folder never holds it."""
    assert [d.uid for d in DASHBOARDS if directory_of(d) == GRAFANA_EXECUTIVE_DIR] == [
        "agentic-executive-roi"
    ]
    assert not (GRAFANA_DIR / "executive-roi.json").exists()
    assert GRAFANA_EXECUTIVE_DIR.parent == GRAFANA_DIR.parent  # a sibling, not a subfolder
    assert set(GRAFANA_DIRS) == {GRAFANA_DIR, GRAFANA_EXECUTIVE_DIR}


def test_panels_use_only_known_macros_and_their_variables():
    for dashboard in DASHBOARDS:
        names = {v.name for v in dashboard.variables}
        sqls = [p.sql for p in dashboard.panels] + [v.sql for v in dashboard.variables]
        for sql in sqls:
            assert set(re.findall(r"\$__(\w+)", sql)) <= set(MACROS), sql
            used = set(re.findall(r"\$(?!__)(\w+)", sql))
            used |= set(re.findall(r"\$\{(\w+)(?::\w+)?\}", sql))
            assert used <= names, (dashboard.uid, sql)


def test_macro_arguments_hold_no_parentheses():
    """Grafana's SQL macro pattern ends a macro's arguments at the first `)`, so an
    expression such as `$__timeFilter(COALESCE(a, b))` would expand to broken SQL."""
    for dashboard in DASHBOARDS:
        for sql in [p.sql for p in dashboard.panels] + [v.sql for v in dashboard.variables]:
            for match in re.finditer(r"\$__\w+\(([^)]*)\)", sql):
                assert "(" not in match.group(1), (dashboard.uid, sql)


def test_panels_read_dash_views_only_through_the_one_datasource():
    for dashboard in DASHBOARDS:
        rendered = render(dashboard)
        for panel in rendered["panels"]:
            assert panel["datasource"] == DATASOURCE
            for target in panel["targets"]:
                assert target["datasource"] == DATASOURCE
                relations = re.findall(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_.]*)", target["rawSql"])
                assert relations and set(relations) <= set(DASH_VIEWS), target["rawSql"]


def test_dashboards_command_is_registered():
    args = build_parser().parse_args(["dashboards", "--check"])
    assert args.check and not args.write
