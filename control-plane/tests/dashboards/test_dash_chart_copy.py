"""The umbrella chart's copy of the dashboards (M3-02): Helm reads only files inside the
chart, so `dashboards --write` refreshes `deploy/helm/agentic-platform/dashboards` too and
`--check` fails while it differs. A dashboard added to the catalog later reaches the chart
with the command its author already runs."""

from pathlib import Path

import pytest

from agentic_cp import __main__ as main
from agentic_cp.dashboards import catalog
from agentic_cp.dashboards.catalog import rendered_files


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    grafana = tmp_path / "grafana"
    chart = tmp_path / "agentic-platform"
    chart.mkdir()
    monkeypatch.setattr(catalog, "GRAFANA_DIR", grafana)
    monkeypatch.setattr(main, "CHART_DASHBOARDS", chart / "dashboards")
    return grafana, chart / "dashboards"


def _run(*flags: str) -> int:
    args = main.build_parser().parse_args(["dashboards", *flags])
    return args.run(args)


def test_the_chart_copy_lives_in_the_source_checkout():
    assert main.CHART_DASHBOARDS.parent.name == "agentic-platform"
    assert (main.CHART_DASHBOARDS.parent / "Chart.yaml").is_file()


def test_check_reports_a_missing_chart_copy_and_write_refreshes_it(dirs, capsys):
    grafana, chart = dirs
    assert _run("--write") == 0
    for name, text in rendered_files().items():
        assert (grafana / name).read_bytes().decode("utf-8") == text
        assert (chart / name).read_bytes().decode("utf-8") == text
    assert _run("--check") == 0
    (chart / "org.json").unlink()
    (chart / "stale.json").write_text("{}", encoding="utf-8")
    capsys.readouterr()
    assert _run("--check") == 1
    error = capsys.readouterr().err
    assert "org.json" in error and "stale.json" in error and "agentic-platform" in error
    assert _run("--write") == 0
    assert not (chart / "stale.json").exists()
    assert _run("--check") == 0


def test_without_the_chart_only_the_catalog_folder_is_kept(tmp_path, monkeypatch):
    # an installed package has no chart next to it
    monkeypatch.setattr(catalog, "GRAFANA_DIR", tmp_path / "grafana")
    monkeypatch.setattr(main, "CHART_DASHBOARDS", tmp_path / "absent" / "dashboards")
    assert _run("--write") == 0
    assert _run("--check") == 0
    assert not (tmp_path / "absent").exists()
