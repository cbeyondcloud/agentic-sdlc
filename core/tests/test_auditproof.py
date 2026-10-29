"""The stdlib-only chain checker and audit export verifier (M2-26, M2-13)."""

from __future__ import annotations

import ast
import csv
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentic import auditproof
from agentic.auditproof import (
    CSV_COLUMNS,
    EXPORT_FORMAT,
    ChainChecker,
    check_chain,
    csv_cell,
    csv_meta_row,
    csv_row,
    csv_uncell,
    entry_hash,
    entry_row,
    header_row,
    jsonl_line,
    link_row,
    proof_row,
    verify_export,
)

TENANT = "7d6b2b1e-0000-4000-8000-000000000001"


def _chain(n: int, *, team_of=lambda seq: "payments", start: tuple[int, str] = (0, "")) -> list:
    """`n` audit records chained after `start` (seq, hash)."""
    records, (seq, prev) = [], start
    for _ in range(n):
        seq += 1
        body = {
            "seq": seq,
            "tenant": TENANT,
            "team": team_of(seq),
            "actor": "lead@example.test",
            "actor_kind": "human",
            "event": "killswitch.set",
            "target": f"killswitch:{seq}",
            "before": None,
            "after": {"mode": "pause", "n": seq, "note": "ünïcödé"},
            "reason": f"reason {seq}",
            "run_id": None,
            "trace_id": None,
            "policy_version": None,
            "at": f"2026-10-20T09:{seq % 60:02d}:00.000Z",
            "prev": prev,
        }
        record = {**body, "hash": entry_hash(body)}
        records.append(record)
        prev = record["hash"]
    return records


def _classes(findings) -> list[tuple[int, str]]:
    return [(f.position, f.field_class) for f in findings]


# ── the chain checker ─────────────────────────────────────────────────────────


def test_intact_chain_has_no_findings():
    assert check_chain(_chain(5)) == []


def test_edited_contents_is_content():
    records = _chain(4)
    records[1]["reason"] = "changed afterwards"
    findings = check_chain(records)
    assert _classes(findings) == [(2, "content")]
    assert findings[0].seq == 2


def test_rewritten_hash_is_hash():
    records = _chain(4)
    records[1]["hash"] = "sha256:" + "0" * 64
    findings = check_chain(records)
    assert _classes(findings) == [(2, "hash")]


def test_edited_and_rehashed_entry_breaks_the_next_link():
    records = _chain(4)
    records[1]["reason"] = "rewritten"
    records[1]["hash"] = entry_hash(records[1])
    assert _classes(check_chain(records)) == [(3, "link")]


def test_removed_entry_is_sequence_and_link():
    records = _chain(5)
    del records[2]
    findings = check_chain(records)
    assert _classes(findings) == [(3, "sequence"), (3, "link")]
    assert findings[0].seq == 4


def test_unreadable_entry_is_located():
    records: list = _chain(3)
    records[1] = None
    assert _classes(check_chain(records)) == [(2, "unreadable")]


def test_the_last_entry_edited_is_content():
    records = _chain(3)
    records[-1]["hash"] = "sha256:" + "1" * 64
    assert _classes(check_chain(records)) == [(3, "content")]


def test_checking_continues_from_an_anchor():
    records = _chain(6)
    tail = records[3:]
    assert check_chain(tail, first_seq=4, prev=records[2]["hash"]) == []
    assert _classes(check_chain(tail, first_seq=4, prev="sha256:" + "9" * 64)) == [(1, "link")]


def test_opaque_link_rows_are_checked_for_links_only():
    records = _chain(4)
    checker = ChainChecker()
    for n, record in enumerate(records):
        if n == 1:
            checker.feed({k: record[k] for k in ("seq", "prev", "hash")}, opaque=True)
        else:
            checker.feed(record)
    assert checker.finish() == []
    assert checker.last == (4, records[-1]["hash"])


def test_a_chain_without_seq_numbers_checks_links_and_hashes():
    records = [{k: v for k, v in r.items() if k != "seq"} for r in _chain(3)]
    prev = ""
    for record in records:
        record["prev"] = prev
        record["hash"] = entry_hash(record)
        prev = record["hash"]
    assert check_chain(records, first_seq=None) == []
    records[0]["actor"] = "someone else"
    assert _classes(check_chain(records, first_seq=None)) == [(1, "content")]


# ── CSV cells ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        '=HYPERLINK("http://x","y")',
        "+1",
        "-1",
        "@SUM(A1)",
        "\tx",
        "\rx",
        "＝cmd",
        "'quoted",
        "plain",
        "",
        "-5",
    ],
)
def test_csv_cells_are_formula_safe_and_reversible(value):
    cell = csv_cell(value)
    assert cell[:1] not in ("=", "+", "-", "@", "\t", "\r", "＝")
    assert csv_uncell(cell) == value


# ── exports ───────────────────────────────────────────────────────────────────


def _jsonl_export(records, *, team=None, start=(0, ""), head=None, links=()) -> bytes:
    rows = [header_row(tenant=TENANT, team=team, start=start, exported_by="a@x.test")]
    entries = link_count = 0
    for record in records:
        if record["seq"] in links:
            rows.append(link_row(record))
            link_count += 1
        else:
            rows.append(entry_row(record))
            entries += 1
    end = (records[-1]["seq"], records[-1]["hash"]) if records else None
    rows.append(proof_row(entries=entries, links=link_count, end=end, head=head or end))
    return b"".join(jsonl_line(row) for row in rows)


def _write(tmp_path: Path, data: bytes, name: str = "export.jsonl") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_verify_jsonl_export(tmp_path):
    records = _chain(5)
    report = verify_export(_write(tmp_path, _jsonl_export(records)))
    assert report.ok, report.findings
    assert (report.entries, report.links) == (5, 0)
    assert report.start == (0, "") and report.end == (5, records[-1]["hash"])
    assert report.format == "jsonl"


def test_unicode_line_separators_inside_a_record_do_not_split_it(tmp_path):
    records = _chain(3)
    records[1]["reason"] = f"one{chr(0x2028)}two{chr(0x85)}three{chr(0x0C)}four"
    records[1]["hash"] = entry_hash(records[1])
    records[2]["prev"] = records[1]["hash"]
    records[2]["hash"] = entry_hash(records[2])
    data = _jsonl_export(records).replace(b"\n", b"\r\n")  # a CRLF copy verifies too
    report = verify_export(_write(tmp_path, data))
    assert report.ok, report.findings


def test_an_edited_byte_in_an_export_is_content(tmp_path):
    data = _jsonl_export(_chain(5)).replace(b"reason 3", b"reason X")
    report = verify_export(_write(tmp_path, data))
    assert not report.ok
    assert [(f.seq, f.field_class) for f in report.findings] == [(3, "content")]


def test_a_dropped_row_breaks_the_link(tmp_path):
    lines = _jsonl_export(_chain(5)).splitlines(keepends=True)
    del lines[3]  # header is line 1, so this is seq 3
    report = verify_export(_write(tmp_path, b"".join(lines)))
    assert {f.field_class for f in report.findings} >= {"link", "sequence"}


def test_an_export_without_its_proof_was_cut_short(tmp_path):
    lines = _jsonl_export(_chain(5)).splitlines(keepends=True)
    report = verify_export(_write(tmp_path, b"".join(lines[:-1])))
    assert [f.field_class for f in report.findings] == ["format"]
    assert "cut short" in report.findings[0].detail


def test_a_proof_with_wrong_counts_is_refused(tmp_path):
    records = _chain(3)
    rows = [header_row(tenant=TENANT, team=None, start=(0, ""), exported_by="a@x.test")]
    rows += [entry_row(record) for record in records]
    rows.append(proof_row(entries=4, links=0, end=(3, records[-1]["hash"]), head=None))
    report = verify_export(_write(tmp_path, b"".join(jsonl_line(row) for row in rows)))
    assert [f.field_class for f in report.findings] == ["anchor"]


def test_a_team_export_carries_other_teams_as_links(tmp_path):
    records = _chain(6, team_of=lambda seq: "payments" if seq % 2 else "billing")
    data = _jsonl_export(records, team="payments", links={2, 4, 6})
    report = verify_export(_write(tmp_path, data))
    assert report.ok, report.findings
    assert (report.entries, report.links, report.team) == (3, 3, "payments")
    assert b"billing" not in data


def test_another_teams_entry_in_a_team_export_is_out_of_scope(tmp_path):
    records = _chain(3, team_of=lambda seq: "billing" if seq == 2 else "payments")
    report = verify_export(_write(tmp_path, _jsonl_export(records, team="payments")))
    assert [(f.seq, f.field_class) for f in report.findings] == [(2, "scope")]


def test_another_tenants_entry_is_out_of_scope(tmp_path):
    records = _chain(2)
    records[1]["tenant"] = "someone-else"
    records[1]["hash"] = entry_hash(records[1])
    report = verify_export(_write(tmp_path, _jsonl_export(records)))
    assert "scope" in {f.field_class for f in report.findings}


def test_an_export_continuing_an_earlier_one(tmp_path):
    records = _chain(8)
    later = records[5:]
    report = verify_export(_write(tmp_path, _jsonl_export(later, start=(5, records[4]["hash"]))))
    assert report.ok and report.start == (5, records[4]["hash"])


def test_an_empty_export_verifies(tmp_path):
    report = verify_export(_write(tmp_path, _jsonl_export([], start=(0, ""))))
    assert report.ok and report.entries == 0 and report.end is None


def test_an_unknown_format_is_refused(tmp_path):
    line = json.dumps({"kind": "header", "format": "something/9"}).encode() + b"\n"
    report = verify_export(_write(tmp_path, line))
    assert [f.field_class for f in report.findings] == ["format"]


def _csv_export(records, links=(), *, team=None, start=None, meta=True) -> bytes:
    """A CSV export as the control plane writes it: column row, header row, records,
    proof row (`meta=False`: the bare table, as an export before review F5)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    if start is None:
        start = (records[0]["seq"] - 1, records[0]["prev"]) if records else (0, "")
    if meta:
        header = header_row(tenant=TENANT, team=team, start=start, exported_by="a@x.test")
        writer.writerow(csv_meta_row(header))
    for record in records:
        writer.writerow(csv_row("link" if record["seq"] in links else "entry", record))
    if meta:
        end = (records[-1]["seq"], records[-1]["hash"]) if records else None
        count = sum(1 for record in records if record["seq"] in links)
        proof = proof_row(entries=len(records) - count, links=count, end=end, head=end)
        writer.writerow(csv_meta_row(proof))
    return out.getvalue().encode("utf-8")


def _csv_lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def test_a_csv_export_cut_short_is_refused(tmp_path):
    """Review F5: a CSV with its tail removed must not verify as intact."""
    lines = _csv_lines(_csv_export(_chain(6)))
    report = verify_export(_write(tmp_path, b"".join(lines[:5]), "export.csv"))
    assert not report.ok
    assert "cut short" in report.findings[-1].detail


def test_a_csv_export_with_its_leading_rows_trimmed_is_refused(tmp_path):
    lines = _csv_lines(_csv_export(_chain(6)))
    report = verify_export(_write(tmp_path, b"".join(lines[:2] + lines[4:]), "export.csv"))
    assert {f.field_class for f in report.findings} >= {"link", "sequence"}


def test_a_csv_export_without_its_header_row_is_refused(tmp_path):
    lines = _csv_lines(_csv_export(_chain(4)))
    report = verify_export(_write(tmp_path, b"".join(lines[:1] + lines[2:]), "export.csv"))
    assert [f.field_class for f in report.findings] == ["format"]


def test_a_bare_csv_table_is_not_a_verified_export(tmp_path):
    report = verify_export(_write(tmp_path, _csv_export(_chain(4), meta=False), "export.csv"))
    assert not report.ok and report.findings[0].field_class == "format"


def test_a_csv_proof_with_wrong_counts_is_refused(tmp_path):
    records = _chain(3)
    data = _csv_export(records).replace(b'""entries"":3', b'""entries"":4')
    report = verify_export(_write(tmp_path, data, "export.csv"))
    assert [f.field_class for f in report.findings] == ["anchor"]


def test_a_csv_export_carries_its_anchors(tmp_path):
    records = _chain(6)
    later = records[3:]
    data = _csv_export(later, team="payments", start=(3, records[2]["hash"]))
    report = verify_export(_write(tmp_path, data, "export.csv"))
    assert report.ok, report.findings
    assert report.start == (3, records[2]["hash"]) and report.end == (6, records[-1]["hash"])
    assert report.head == report.end and report.team == "payments"


def test_verify_csv_export(tmp_path):
    records = _chain(4)
    records[1]["reason"] = '=HYPERLINK("http://evil","click")'
    records[2]["team"] = None
    records[3]["trace_id"] = ""  # an empty string, not null: still verifies
    prev = ""
    for record in records:
        record["prev"] = prev
        record["hash"] = entry_hash({k: v for k, v in record.items() if k != "hash"})
        prev = record["hash"]
    data = _csv_export(records, links={3}, team="payments")
    assert b"'=HYPERLINK" in data
    report = verify_export(_write(tmp_path, data, "export.csv"))
    assert report.ok, report.findings
    assert (report.format, report.entries, report.links) == ("csv", 3, 1)


def test_an_edited_csv_cell_is_content(tmp_path):
    data = _csv_export(_chain(3)).replace(b"reason 2", b"reason 9")
    report = verify_export(_write(tmp_path, data, "export.csv"))
    assert [(f.seq, f.field_class) for f in report.findings] == [(2, "content")]


def test_verifier_runs_standalone(tmp_path):
    """An auditor copies the one file and runs it with nothing of ours importable."""
    copy = tmp_path / "verify_audit_export.py"
    shutil.copyfile(auditproof.__file__, copy)
    good = _write(tmp_path, _jsonl_export(_chain(4)))
    done = subprocess.run(
        [sys.executable, "-I", str(copy), str(good)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert "intact" in done.stdout
    bad = _write(tmp_path, good.read_bytes().replace(b"reason 2", b"reason 7"), "bad.jsonl")
    done = subprocess.run(
        [sys.executable, "-I", str(copy), str(bad)], capture_output=True, text=True
    )
    assert done.returncode == 3
    assert "seq 2" in done.stdout and "content" in done.stdout


def test_main_usage_and_unreadable_files(tmp_path, capsys):
    assert auditproof.main([]) == 64
    assert auditproof.main([str(tmp_path / "missing.jsonl")]) == 1


def test_header_names_the_format():
    row = header_row(tenant=TENANT, team=None, start=(0, ""), exported_by="a@x.test")
    assert row["format"] == EXPORT_FORMAT and row["kind"] == "header"


def test_auditproof_imports_only_the_standard_library():
    tree = ast.parse(Path(auditproof.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_auditproof_runs_on_the_system_python_of_older_machines():
    """DF-612: an auditor's macOS (Command Line Tools) or Ubuntu 22.04 has Python 3.9 or
    3.10: no 3.10+ syntax, and none of the 3.10/3.11 names that fail on import."""
    source = Path(auditproof.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))  # raises on match, except*, ...
    assert "from __future__ import annotations" in source  # `X | None` stays unevaluated
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            assert "UTC" not in {alias.name for alias in node.names}  # 3.11+
        if isinstance(node, ast.Call):
            assert "strict" not in {kw.arg for kw in node.keywords}, ast.unparse(node)  # 3.10+
            if isinstance(node.func, ast.Name) and node.func.id == "dataclass":
                assert not {"slots", "kw_only"} & {kw.arg for kw in node.keywords}
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"pairwise", "UTC"}, ast.unparse(node)
    assert "3.9" in source.split('"""', 2)[1]  # the docstring says what it needs
