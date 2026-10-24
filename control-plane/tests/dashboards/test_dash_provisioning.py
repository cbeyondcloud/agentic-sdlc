"""Grafana provisioning (M2-38, Review Focus 5): the providers load every dashboard, every
panel's datasource is provisioned, and every panel's query - with Grafana's macros and the
dashboard's variables expanded as Grafana expands them - returns rows on the seeded
database, read as the tenant's dashboard reader exactly as Grafana would."""

import json
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from grafana_macros import MacroError, TimeRange, expand
from pg import REPO_ROOT

from agentic.yamlio import load_yaml
from agentic_cp.dashboards.catalog import DASHBOARDS, GRAFANA_DIR

pytestmark = pytest.mark.anyio

PROVISIONING = REPO_ROOT / "deploy" / "org-local" / "grafana" / "provisioning"
#: The single-value variable each dashboard is opened with (Grafana's default is the first
#: option; the test picks a run with steps, gates and calls, after checking it is offered).
CHOSEN = {"run": "a-r1"}


def _yaml(path):
    data, _ = load_yaml(path.read_text(encoding="utf-8"))
    return data


def _uids_in(value) -> set[str]:
    """Every datasource uid a dashboard JSON names."""
    found: set[str] = set()
    if isinstance(value, dict):
        source = value.get("datasource")
        if isinstance(source, dict) and source.get("uid"):
            found.add(source["uid"])
        for item in value.values():
            found |= _uids_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= _uids_in(item)
    return found


def test_providers_load_every_dashboard():
    providers = _yaml(PROVISIONING / "dashboards" / "agentic.yaml")["providers"]
    paths = {p["name"]: p["options"]["path"] for p in providers}
    assert paths == {
        "agentic": "/var/lib/grafana/dashboards/agentic",
        "gateway": "/var/lib/grafana/dashboards/gateway",
    }
    files = {path.name for path in GRAFANA_DIR.glob("*.json")}
    assert files == {f"{d.uid.removeprefix('agentic-')}.json" for d in DASHBOARDS}
    for name in files:
        loaded = json.loads((GRAFANA_DIR / name).read_text(encoding="utf-8"))
        assert loaded["uid"] and loaded["panels"], name


def test_every_datasource_uid_is_provisioned():
    provisioned = {
        source["uid"]
        for path in (PROVISIONING / "datasources").glob("*.yaml")
        for source in _yaml(path)["datasources"]
    }
    assert provisioned == {"agentic-postgres", "app-postgres"}
    for path in GRAFANA_DIR.glob("*.json"):
        assert _uids_in(json.loads(path.read_text(encoding="utf-8"))) == {"agentic-postgres"}


def test_datasources_take_secrets_from_the_environment_only():
    for path in (PROVISIONING / "datasources").glob("*.yaml"):
        for source in _yaml(path)["datasources"]:
            for key in ("url", "user"):
                assert source[key].startswith("${"), (path.name, key)
            assert source["secureJsonData"]["password"].startswith("${"), path.name
            assert source["jsonData"]["database"].startswith("${"), path.name
            assert source["editable"] is False


def test_macro_expansion_refuses_what_grafana_cannot_expand():
    window = TimeRange(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
    assert expand("WHERE $__timeFilter(at)", window, {}) == (
        "WHERE at BETWEEN '2026-01-01T00:00:00+00:00' AND '2026-01-02T00:00:00+00:00'"
    )
    assert expand("team IN ($team)", window, {"team": ["a", "o'b"]}) == "team IN ('a','o''b')"
    assert expand("r = ${run:sqlstring}", window, {"run": ["x"]}) == "r = 'x'"
    for bad in (
        "WHERE $__timeFilter(COALESCE(a, b))",
        "WHERE $__unixEpochFilter(at)",
        "WHERE x = $missing",
        "WHERE x = ${run:csv}",
    ):
        with pytest.raises(MacroError):
            expand(bad, window, {"run": ["x"]})


@pytest.fixture
async def reader_a(readers):
    """Tenant a's dashboard reader, connected as Grafana connects."""
    _, seeded, made = readers
    role, password = made.name("grafana"), secrets.token_hex(12)
    await made.bind(seeded.a, role, password)
    return await made.connect(role, password)


async def _options(conn, sql: str, window: TimeRange) -> list[str]:
    rows = await conn.fetch(expand(sql, window, {}))
    return [str(row[0]) for row in rows]


async def test_every_panel_query_returns_rows(reader_a):
    from dash_seed import NOW

    window = TimeRange(NOW - timedelta(days=30), datetime.now(UTC) + timedelta(hours=1))
    checked = 0
    for dashboard in DASHBOARDS:
        values: dict[str, list[str]] = {}
        for variable in dashboard.variables:
            options = await _options(reader_a, variable.sql, window)
            assert options, (dashboard.uid, variable.name)
            if variable.multi:  # opened with "All": every option
                values[variable.name] = options
            else:
                assert CHOSEN[variable.name] in options, (dashboard.uid, variable.name)
                values[variable.name] = [CHOSEN[variable.name]]
        for panel in dashboard.panels:
            rows = await reader_a.fetch(expand(panel.sql, window, values))
            where = f"{dashboard.uid}: {panel.title}"
            assert rows, f"{where} returned no rows"
            assert any(value is not None for value in rows[0].values()), where
            if panel.kind == "stat":
                assert len(rows) == 1 and rows[0][0] is not None, where
            checked += 1
    assert checked == sum(len(d.panels) for d in DASHBOARDS)
