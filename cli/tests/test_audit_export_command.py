"""`agentic audit export` and `agentic audit verify --export` (M2-13): the export is
streamed to a file (written whole or not at all), verified with the auditor's stdlib
verifier, and its anchors reported; a refusal exits 2 and a tampered file exits 3."""

from __future__ import annotations

import json

import httpx2
import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic_cli import control_plane
from agentic_cli.config import agentic_home
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main


@pytest.fixture
def plane(home, monkeypatch, tmp_path) -> FakeControlPlane:
    fake = FakeControlPlane(allowed={"audit.read.org", "audit.export"})
    fake.add_audit(6, team_of=lambda seq: "payments" if seq % 2 else "billing")
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
    code = main(["--json", "audit", *args])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def test_export_writes_and_verifies(plane, capsys, tmp_path):
    code, data = run(capsys, "export", "--team", "payments", "--from", "2026-10-01")
    assert code == ExitCode.OK, data
    path = tmp_path / data["path"]
    assert path.name == "audit-payments-2026-10-01-now.jsonl" and path.is_file()
    assert data["intact"] and (data["entries"], data["links"]) == (3, 3)
    assert data["end"]["seq"] == 6 and data["team"] == "payments"
    [sent] = [r for r in plane.requests if r.url.path == "/v1/audit/export"]
    assert dict(sent.url.params) == {"format": "jsonl", "team": "payments", "from": "2026-10-01"}
    assert not list(tmp_path.glob("*.part"))


def test_export_csv_to_a_named_file(plane, capsys, tmp_path):
    target = tmp_path / "out" / "q3.csv"
    target.parent.mkdir()
    code, data = run(capsys, "export", "--format", "csv", "--output", str(target))
    assert code == ExitCode.OK, data
    assert target.read_bytes().startswith(b"kind,seq,")
    assert data["format"] == "csv" and data["intact"]


def test_export_refused_exits_2(plane, capsys, tmp_path):
    plane.allowed = {"audit.read.org"}
    code = main(["audit", "export"])
    assert code == ExitCode.REFUSED
    assert "audit.export" in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == []


def test_a_cut_stream_is_reported_broken(plane, capsys, tmp_path):
    plane.export_cut = True
    code, data = run(capsys, "export")
    assert code == ExitCode.INVALID
    assert not data["intact"]
    assert any("cut short" in f["detail"] for f in data["findings"])


def test_a_cut_csv_stream_is_reported_broken(plane, capsys, tmp_path):
    """Review F5: a CSV export carries its proof too, so a cut one is not 'intact'."""
    plane.export_cut = True
    code, data = run(capsys, "export", "--format", "csv")
    assert code == ExitCode.INVALID
    assert data["format"] == "csv" and not data["intact"]
    assert any("cut short" in f["detail"] for f in data["findings"])


class _Broken(httpx2.SyncByteStream):
    def __iter__(self):
        yield b'{"kind":"header"}\n'
        raise httpx2.ReadError("connection reset")


def test_a_failed_download_leaves_no_file(plane, capsys, tmp_path, monkeypatch):
    original = plane.handle

    def handle(request):
        if request.url.path == "/v1/audit/export":
            return httpx2.Response(200, stream=_Broken())
        return original(request)

    monkeypatch.setattr(plane, "handle", handle)
    code = main(["audit", "export", "--output", "export.jsonl"])
    assert code == ExitCode.ERROR
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == []


def test_an_existing_file_is_not_overwritten(plane, capsys, tmp_path):
    (tmp_path / "keep.jsonl").write_text("mine", encoding="utf-8")
    assert main(["audit", "export", "--output", "keep.jsonl"]) == ExitCode.REFUSED
    assert (tmp_path / "keep.jsonl").read_text(encoding="utf-8") == "mine"


def test_verify_export_flags_tampering(plane, capsys, tmp_path):
    code, data = run(capsys, "export", "--output", "a.jsonl")
    assert code == ExitCode.OK
    path = tmp_path / "a.jsonl"
    code, data = run(capsys, "verify", "--export", str(path))
    assert code == ExitCode.OK and data["exports"][0]["intact"] and data["runs"] == []
    path.write_bytes(path.read_bytes().replace(b"reason 4", b"reason 9"))
    code, data = run(capsys, "verify", "--export", str(path))
    assert code == ExitCode.INVALID
    assert [(f["seq"], f["field_class"]) for f in data["exports"][0]["findings"]] == [
        (4, "content")
    ]


def test_human_export_output_names_the_anchors(plane, capsys, tmp_path):
    assert main(["audit", "export", "--output", "h.jsonl"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "h.jsonl: intact" in out and "end seq 6" in out
