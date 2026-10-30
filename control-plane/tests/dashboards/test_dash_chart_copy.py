"""The umbrella chart's copy of the dashboards (M3-02): Helm reads only files inside the
chart, so `dashboards --write` refreshes the chart's copy of each Grafana folder too
(`dashboards` for `grafana`, `dashboards-executive` for `grafana-executive`) and `--check`
fails while it differs. A dashboard added to the catalog later reaches the chart with the
command its author already runs."""

from pathlib import Path

import pytest

from agentic_cp import __main__ as main
from agentic_cp.dashboards import catalog
from agentic_cp.dashboards.catalog import rendered_files


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    chart = tmp_path / "agentic-platform"
    chart.mkdir()
    _catalog_in(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "CHART_DASHBOARDS", chart / "dashboards")
    return tmp_path, chart


def _catalog_in(root: Path, monkeypatch) -> None:
    shared, executive = root / "grafana", root / "grafana-executive"
    monkeypatch.setattr(catalog, "GRAFANA_DIR", shared)
    monkeypatch.setattr(catalog, "GRAFANA_EXECUTIVE_DIR", executive)
    monkeypatch.setattr(catalog, "GRAFANA_DIRS", (shared, executive))


def _run(*flags: str) -> int:
    args = main.build_parser().parse_args(["dashboards", *flags])
    return args.run(args)


def test_the_chart_copy_lives_in_the_source_checkout():
    assert main.CHART_DASHBOARDS.parent.name == "agentic-platform"
    assert (main.CHART_DASHBOARDS.parent / "Chart.yaml").is_file()


def test_check_reports_a_missing_chart_copy_and_write_refreshes_it(dirs, capsys):
    root, chart_root = dirs
    chart = chart_root / "dashboards"
    assert _run("--write") == 0
    for folder, copy in (("grafana", "dashboards"), ("grafana-executive", "dashboards-executive")):
        files = rendered_files(root / folder)
        assert files
        for name, text in files.items():
            assert (root / folder / name).read_bytes().decode("utf-8") == text
            assert (chart_root / copy / name).read_bytes().decode("utf-8") == text
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


def test_per_person_cost_stays_out_of_the_shared_chart_folder(dirs):
    # review F3 of M3-01: Executive ROI shows each person's cost, so the chart keeps it in
    # its own folder, which the template provisions into the restricted Grafana folder
    root, chart_root = dirs
    assert _run("--write") == 0
    executive = set(rendered_files(root / "grafana-executive"))
    assert executive
    assert {p.name for p in (chart_root / "dashboards-executive").glob("*.json")} == executive
    assert not executive & {p.name for p in (chart_root / "dashboards").glob("*.json")}


def test_without_the_chart_only_the_catalog_folder_is_kept(tmp_path, monkeypatch):
    # an installed package has no chart next to it
    _catalog_in(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "CHART_DASHBOARDS", tmp_path / "absent" / "dashboards")
    assert _run("--write") == 0
    assert _run("--check") == 0
    assert not (tmp_path / "absent").exists()
