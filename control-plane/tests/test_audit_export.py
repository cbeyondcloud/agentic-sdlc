"""`GET /v1/audit/export` (M2-13): auditors (and org admins) export a range of the tenant's
audit chain as JSONL or CSV with a chain proof that the stdlib verifier checks without the
database; team leads and developers are refused; another team's records travel as links
only; CSV is safe in a spreadsheet; the body is streamed; every export is audited."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import timedelta

import pytest
from apiclient import member

from agentic import auditproof
from agentic.auditproof import verify_export
from agentic_cp.audit import read_records
from agentic_cp.audit.export import CHUNK_ROWS, ExportRange, parse_bound, stream_export
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


async def _switch(api, team: str, reason: str = "drill") -> None:
    made = await api.post(
        "/v1/killswitch",
        api.a.admin_token,
        {"scope_kind": "team", "scope_value": team, "mode": "pause", "reason": reason},
    )
    assert made.status_code == 201, made.text


async def _export(api, token, query: str = "", fmt: str = "jsonl"):
    sep = "&" if query else ""
    return await api.get(f"/v1/audit/export?format={fmt}{sep}{query}", token)


def _rows(body: bytes) -> list[dict]:
    return [json.loads(line) for line in body.decode("utf-8").split("\n") if line]


async def test_only_auditors_export(api):
    auditor, _ = await member(api, api.a, "aud@a.example.test", "auditor")
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    approver, _ = await member(api, api.a, "ok@a.example.test", "approver", "payments")
    assert (await _export(api, auditor)).status_code == 200
    assert (await _export(api, auditor, "team=payments")).status_code == 200
    assert (await _export(api, api.a.admin_token)).status_code == 200
    for token in (lead, developer, approver):
        for query in ("", "team=payments"):
            refused = await _export(api, token, query)
            assert refused.status_code == 403, (query, refused.text)
            assert refused.json()["code"] == "rbac.forbidden"


async def test_org_export_round_trip(api, tmp_path):
    await _switch(api, "payments")
    await _switch(api, "billing")
    reply = await _export(api, api.a.admin_token)
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in reply.headers["content-disposition"]
    rows = _rows(reply.content)
    assert rows[0]["kind"] == "header" and rows[0]["tenant"] == str(api.a.id)
    assert rows[0]["start"] == {"seq": 0, "hash": ""} and rows[-1]["kind"] == "proof"
    async with tenant_tx(api.db.app, api.a.id) as conn:
        stored = await read_records(conn)
    exported = [row["record"] for row in rows if row["kind"] == "entry"]
    # everything before the export itself (`audit.exported` comes after the range)
    assert [r["seq"] for r in exported] == [r.seq for r in stored][: len(exported)]
    assert stored[len(exported)].event == "audit.exported"
    path = tmp_path / "org.jsonl"
    path.write_bytes(reply.content)
    report = verify_export(path)
    assert report.ok, report.findings
    assert report.end == (exported[-1]["seq"], exported[-1]["hash"])


async def test_team_export_hides_other_teams(api, tmp_path):
    await _switch(api, "payments", "pay drill")
    await _switch(api, "billing", "secret billing incident")
    await _switch(api, "payments", "pay drill 2")
    auditor, _ = await member(api, api.a, "aud@a.example.test", "auditor")
    reply = await _export(api, auditor, "team=payments")
    assert reply.status_code == 200
    assert b"secret billing incident" not in reply.content
    rows = _rows(reply.content)
    assert {row["record"]["team"] for row in rows if row["kind"] == "entry"} == {"payments"}
    links = [row for row in rows if row["kind"] == "link"]
    assert links and all(set(row) == {"kind", "seq", "prev", "hash"} for row in links)
    path = tmp_path / "team.jsonl"
    path.write_bytes(reply.content)
    report = verify_export(path)
    assert report.ok, report.findings
    assert report.team == "payments" and report.links == len(links)


async def test_range_by_date_and_datetime(api, tmp_path):
    day = api.clock.now.replace(hour=12, minute=0, second=0)
    api.clock.now = day
    await _switch(api, "payments", "day one")
    api.clock.advance(timedelta(days=1))
    await _switch(api, "payments", "day two")
    api.clock.advance(timedelta(days=1))
    await _switch(api, "payments", "day three")
    second = (day + timedelta(days=1)).date().isoformat()
    reply = await _export(api, api.a.admin_token, f"from={second}&to={second}")
    reasons = [r["record"]["reason"] for r in _rows(reply.content) if r["kind"] == "entry"]
    assert reasons == ["day two"]
    start = (day + timedelta(days=1)).isoformat()
    end = (day + timedelta(days=1, minutes=1)).isoformat()
    reply = await _export(api, api.a.admin_token, f"from={start}&to={end}".replace("+", "%2B"))
    reasons = [r["record"]["reason"] for r in _rows(reply.content) if r["kind"] == "entry"]
    assert reasons == ["day two"]
    path = tmp_path / "range.jsonl"
    path.write_bytes(reply.content)
    report = verify_export(path)
    assert report.ok and report.start[0] > 0, report.findings


async def test_an_empty_range_is_a_valid_export(api, tmp_path):
    reply = await _export(api, api.a.admin_token, "from=2001-01-01&to=2001-01-02")
    path = tmp_path / "empty.jsonl"
    path.write_bytes(reply.content)
    report = verify_export(path)
    assert report.ok and report.entries == 0, report.findings


@pytest.mark.parametrize(
    "query",
    ["from=yesterday", "to=2026-13-01", "from=2026-10-02&to=2026-10-01", "from=2026-10-01T10:00"],
)
async def test_invalid_range_is_422(api, query):
    reply = await _export(api, api.a.admin_token, query)
    assert reply.status_code == 422
    assert reply.json()["code"] == "audit.invalid_range"


async def test_unknown_format_is_422(api):
    assert (await _export(api, api.a.admin_token, fmt="xml")).status_code == 422


async def test_export_is_audited(api):
    auditor, _ = await member(api, api.a, "aud@a.example.test", "auditor")
    await _export(api, auditor, "team=payments&from=2026-10-01", fmt="csv")
    async with tenant_tx(api.db.app, api.a.id) as conn:
        last = (await read_records(conn))[-1]
    assert (last.event, last.actor, last.team) == (
        "audit.exported",
        "aud@a.example.test",
        "payments",
    )
    assert last.after["format"] == "csv" and last.after["from"] == "2026-10-01T00:00:00.000Z"


async def test_csv_export_is_formula_safe(api, tmp_path):
    await _switch(api, "payments", '=HYPERLINK("http://evil.example","click")')
    await _switch(api, "payments", "@SUM(A1:A9)")
    reply = await _export(api, api.a.admin_token, fmt="csv")
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("text/csv")
    text = reply.content.decode("utf-8")
    assert "'=HYPERLINK" in text and "'@SUM" in text
    assert ",=HYPERLINK" not in text and ',"=HYPERLINK' not in text
    path = tmp_path / "export.csv"
    path.write_bytes(reply.content)
    report = verify_export(path)
    assert report.ok, report.findings
    assert report.format == "csv"


async def test_export_verifies_independently(api, tmp_path):
    """Review Focus 2: the copied stdlib verifier, run with nothing of ours importable."""
    await _switch(api, "payments")
    reply = await _export(api, api.a.admin_token)
    export = tmp_path / "audit.jsonl"
    export.write_bytes(reply.content)
    copy = tmp_path / "verify_audit_export.py"
    shutil.copyfile(auditproof.__file__, copy)
    done = subprocess.run([sys.executable, "-I", str(copy), str(export)], capture_output=True)
    assert done.returncode == 0, done.stdout
    export.write_bytes(reply.content.replace(b"drill", b"drull", 1))
    done = subprocess.run(
        [sys.executable, "-I", str(copy), str(export)], capture_output=True, text=True
    )
    assert done.returncode == 3 and "content" in done.stdout


async def test_large_export_streams(api):
    """The body is produced chunk by chunk from a server-side cursor, never built whole."""
    async with tenant_tx(api.db.app, api.a.id) as conn:
        from agentic_cp.audit import AuditEvent, append

        for n in range(CHUNK_ROWS * 3):
            await append(
                conn,
                AuditEvent(
                    event="test.bulk", actor="system:test", actor_kind="system", target=str(n)
                ),
                clock=api.clock,
            )
        head = (await read_records(conn))[-1]
    rng = ExportRange(first_seq=1, last_seq=head.seq, start=(0, ""), head=(head.seq, head.hash))
    chunks = [
        chunk
        async for chunk in stream_export(
            api.db.app, api.a.id, rng, team=None, fmt="jsonl", header={"exported_by": "x"}
        )
    ]
    assert len(chunks) >= 3
    assert sum(chunk.count(b"\n") for chunk in chunks) == head.seq + 2  # header and proof


def test_parse_bound():
    assert parse_bound("2026-10-01", end=False) == "2026-10-01T00:00:00.000Z"
    assert parse_bound("2026-10-01", end=True) == "2026-10-02T00:00:00.000Z"
    assert parse_bound("2026-10-01T10:30:00+02:00", end=True) == "2026-10-01T08:30:00.000Z"
    assert parse_bound("2026-10-01T10:30:00Z", end=False) == "2026-10-01T10:30:00.000Z"
    assert parse_bound(None, end=True) is None
    for bad in ("2026-10-01T10:30", "soon", "2026-02-30"):
        with pytest.raises(ValueError):
            parse_bound(bad, end=False)
