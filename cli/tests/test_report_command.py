"""`agentic report chargeback` (M3-01): the month's chargeback per team from the control
plane, written to a CSV file whole or not at all (never over an existing file), its totals
shown; a refusal exits 2; outside org mode it is refused with a hint."""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal

import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic.orgapi.cost import CSV_COLUMNS
from agentic_cli import control_plane
from agentic_cli.config import agentic_home
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main


def _row(team: str, cost: str | None, month: str = "2026-09") -> dict:
    return {
        "month": month,
        "team": team,
        "users": 2,
        "runs": 3,
        "step_executions": 9,
        "calls": 12,
        "input_tokens": 12000,
        "output_tokens": 2400,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "tokens": 14400,
        "attributed_usd": cost,
        "unattributed_usd": None,
        "cost_usd": cost,
    }


@pytest.fixture
def plane(home, monkeypatch, tmp_path) -> FakeControlPlane:
    fake = FakeControlPlane(allowed={"dashboard.view.org"})
    fake.chargeback = [
        _row("payments", "1.250000"),
        _row("search", None),
        _row("payments", "9.000000", month="2026-10"),
    ]
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    config = agentic_home()
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        f"org: acme\ncontrol_plane_url: {CP_URL}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    return fake


def run(capsys, *args: str) -> tuple[int, dict]:
    code = main(["--json", "report", *args])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def test_chargeback_writes_the_csv_and_reports_totals(plane, capsys, tmp_path):
    code, data = run(capsys, "chargeback", "--month", "2026-09")
    assert code == ExitCode.OK, data
    path = tmp_path / "chargeback-org-2026-09.csv"
    assert data["path"] == str(path.name) and path.is_file()
    assert data["month"] == "2026-09" and data["teams"] == 2
    assert data["totals"]["cost_usd"] == "1.250000"
    raw = path.read_bytes()
    assert b"\r" not in raw  # the same bytes on Windows and macOS
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert sum(Decimal(r["cost_usd"]) for r in rows if r["cost_usd"]) == Decimal("1.25")
    assert rows[1]["cost_usd"] == ""  # unmeasured: empty, never 0
    [sent] = [r for r in plane.requests if r.url.path == "/v1/cost/chargeback"]
    assert dict(sent.url.params) == {"month": "2026-09"}
    assert not list(tmp_path.glob("*.part"))


def test_one_team_to_a_named_file(plane, capsys, tmp_path):
    target = tmp_path / "out" / "payments.csv"
    target.parent.mkdir()
    code, data = run(
        capsys, "chargeback", "--month", "2026-10", "--team", "payments", "-o", str(target)
    )
    assert code == ExitCode.OK, data
    assert data["team"] == "payments" and data["totals"]["cost_usd"] == "9.000000"
    assert target.read_text(encoding="utf-8").count("\n") == 2


def test_an_existing_file_is_never_overwritten(plane, capsys, tmp_path):
    (tmp_path / "chargeback-org-2026-09.csv").write_text("keep", encoding="utf-8")
    code = main(["report", "chargeback", "--month", "2026-09"])
    assert code == ExitCode.REFUSED
    assert "already exists" in capsys.readouterr().err
    assert (tmp_path / "chargeback-org-2026-09.csv").read_text(encoding="utf-8") == "keep"


def test_refused_without_the_permission(plane, capsys, tmp_path):
    plane.allowed = {"dashboard.view.own"}
    code = main(["report", "chargeback", "--month", "2026-09"])
    assert code == ExitCode.REFUSED
    assert "dashboard.view.org" in capsys.readouterr().err
    assert not [p for p in tmp_path.iterdir() if p.is_file()]


@pytest.mark.parametrize("month", ["2026-13", "Sept", "2026-9"])
def test_month_must_be_yyyy_mm(plane, capsys, month):
    code = main(["report", "chargeback", "--month", month])
    assert code == ExitCode.USAGE
    assert "YYYY-MM" in capsys.readouterr().err
    assert not [r for r in plane.requests if r.url.path == "/v1/cost/chargeback"]


def test_outside_org_mode_is_refused_with_a_hint(home, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    code = main(["report", "chargeback"])
    assert code == ExitCode.REFUSED
    assert "org mode" in capsys.readouterr().err
