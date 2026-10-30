"""What `agentic audit` checks (M2-26, M2-13): local run ledgers, the control plane's
per-tenant audit chain, and audit export files.

The verdict on a local run is Zyroflo's (`RunLedger.verify`, through `sdlc_commons`);
`agentic.auditproof` then locates each problem line by line and names its class of change
(content, hash, link, sequence, unreadable), the same code an auditor runs on an export.
The control plane's chain is pulled page by page and checked here, so the verdict never
rests on the server's own check."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agentic.auditproof import ChainChecker, ExportReport, Finding, verify_export
from agentic.engine.state import RunStateError, RunStore
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from sdlc_commons.ledger import LedgerError, RunLedger, run_id_refusal, run_ids
from sdlc_commons.lifecycle import RUNS_ROOT

#: Records per page when pulling the control plane's chain (the server's maximum).
PAGE = 1000
#: A running run's half-written last line is read again after this long (DF-618): an
#: append finishes in far less, so a line still torn then is the run being written.
TORN_RETRY_S = 0.2
ACTIVE_NOTE = (
    "run is active: its last event is still being written; checked up to the last whole line"
)
_sleep = time.sleep  # replaced by the tests


def _finding(file: str, finding: Finding) -> dict:
    return {
        "file": file,
        "entry": finding.position,
        "seq": finding.seq,
        "field_class": finding.field_class,
        "detail": finding.detail,
    }


@dataclass
class RunReport:
    """One local run: located findings per chain file, and Zyroflo's own problems."""

    run_id: str
    entries: int = 0
    findings: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    active: bool = False  # running, its last line half written: checked to the last whole one
    note: str = ""

    @property
    def intact(self) -> bool:
        return not self.findings and not self.problems

    def as_dict(self) -> dict:
        data = {
            "run_id": self.run_id,
            "intact": self.intact,
            "active": self.active,
            "entries": self.entries,
            "findings": self.findings,
            "problems": self.problems,
        }
        if self.note:
            data["note"] = self.note
        return data


def _lines(path: Path) -> list[object]:
    """Each line of a JSONL chain file as parsed (None for a line that is not an object);
    LF splits lines, so a record's own U+2028 never does."""
    entries: list[object] = []
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        entries.append(value if isinstance(value, dict) else None)
    return entries


def verify_run(repo: Path, run_id: str) -> RunReport:
    """One run's chains. A running run whose ledger ends in a half-written line is read
    again once; still torn, it is checked up to its last whole line and reported active,
    never BROKEN (DF-618). A finished run's torn tail is a problem, as before."""
    ledger = RunLedger(repo, run_id)
    if _running(repo, run_id) and _torn_tail(ledger):
        _sleep(TORN_RETRY_S)
        if _torn_tail(ledger):
            return _verify_whole_lines(repo, run_id, ledger)
    return _verify(ledger, RunReport(run_id))


def _running(repo: Path, run_id: str) -> bool:
    try:
        return RunStore(Path(repo) / RUNS_ROOT).load(run_id).status == "running"
    except RunStateError:
        return False


def _torn_tail(ledger: RunLedger) -> bool:
    """Whether a chain file ends in a line that is not (yet) a whole record."""
    for path in ledger.chain_files():
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not data or data.endswith(b"\n"):
            continue
        try:
            whole = isinstance(json.loads(data.rsplit(b"\n", 1)[-1]), dict)
        except ValueError:
            whole = False
        if not whole:
            return True
    return False


def _verify_whole_lines(repo: Path, run_id: str, ledger: RunLedger) -> RunReport:
    """Verify a snapshot of the run's chains cut at their last newline (the run goes on
    writing the live files meanwhile; they are never changed here)."""
    with tempfile.TemporaryDirectory(prefix="agentic-verify-") as folder:
        snapshot = Path(folder)
        for path in ledger.chain_files():
            if not path.is_file():
                continue
            data = path.read_bytes()
            copy = snapshot / path.relative_to(repo)
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(data[: data.rfind(b"\n") + 1])
        return _verify(
            RunLedger(snapshot, run_id), RunReport(run_id, active=True, note=ACTIVE_NOTE)
        )


def _verify(ledger: RunLedger, report: RunReport) -> RunReport:
    try:
        report.problems = ledger.verify()
    except LedgerError as error:
        report.problems = [str(error)]
    events, approvals = ledger.chain_files()
    for path, first_seq in ((events, 1), (approvals, None)):
        if not path.is_file():
            continue
        entries = _lines(path)
        if path == events:
            report.entries = len(entries)
        checker = ChainChecker(first_seq=first_seq)
        for entry in entries:
            checker.feed(entry)
        report.findings += [_finding(path.name, f) for f in checker.finish()]
    return report


def verify_runs(repo: Path, wanted: Sequence[str] = ()) -> list[RunReport]:
    """Every run of the repository (or the `wanted` ones), sorted by run id."""
    known = run_ids(repo)
    for run_id in wanted:
        if why := run_id_refusal(run_id):
            raise CliError(why, ExitCode.USAGE)
        if run_id not in known:
            raise CliError(
                f"no run {run_id} in this repository",
                ExitCode.USAGE,
                hint="`agentic audit verify` with no run id checks every run",
            )
    return [verify_run(repo, run_id) for run_id in (sorted(set(wanted)) or known)]


@dataclass
class ChainReport:
    """The control plane's audit chain as this CLI checked it."""

    tenant: str
    records: int = 0
    head: tuple[int, str] | None = None
    findings: list[dict] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict:
        return {
            "tenant": self.tenant,
            "intact": self.intact,
            "records": self.records,
            "head": None if self.head is None else {"seq": self.head[0], "hash": self.head[1]},
            "findings": self.findings,
        }


#: The event the control plane's retention job writes before it prunes the chain's oldest
#: records (M3-06); `after.audit` names the last record pruned (`through_seq`, `through_hash`).
PRUNED = "retention.pruned"


def _names(record: Mapping, start: tuple[int, str]) -> bool:
    """Whether `record` is a retention checkpoint naming `start` (seq and hash)."""
    after = record.get("after")
    audit = (
        after.get("audit") if record.get("event") == PRUNED and isinstance(after, Mapping) else None
    )
    return (
        isinstance(audit, Mapping)
        and (audit.get("through_seq"), audit.get("through_hash")) == start
    )


def verify_control_plane(client, tenant: str) -> ChainReport:
    """Pull the caller's tenant's audit chain (`audit.read.org`) and check it here. A chain
    whose oldest records retention pruned starts later than seq 1: it is checked from its
    first record, which must be anchored by a `retention.pruned` checkpoint in the chain
    naming the record before it and that record's hash."""
    report = ChainReport(tenant)
    checker: ChainChecker | None = None
    start: tuple[int, str] | None = None  # the pruned anchor the chain must name
    anchored = False
    after = 0
    while True:
        page = client.audit_records(after_seq=after, limit=PAGE)
        for record in page:
            entry = record.model_dump(mode="json")
            if checker is None:
                seq = entry.get("seq")
                if isinstance(seq, int) and seq > 1:
                    start = (seq - 1, entry.get("prev", ""))
                    checker = ChainChecker(first_seq=seq, prev=start[1])
                else:
                    checker = ChainChecker()
            anchored = anchored or (start is not None and _names(entry, start))
            checker.feed(entry)
        report.records += len(page)
        if len(page) < PAGE:
            break
        after = page[-1].seq
    checker = checker or ChainChecker()
    found = checker.finish()
    if start is not None and not anchored:
        found.insert(
            0,
            Finding(
                1,
                start[0] + 1,
                "sequence",
                f"the records before seq {start[0] + 1} are missing and no retention "
                "checkpoint names them - records were removed",
            ),
        )
    report.findings = [_finding("audit chain", f) for f in found]
    if report.records:
        seq, digest = checker.last
        report.head = (seq or 0, digest)
    return report


def export_name(team: str | None, start: str | None, end: str | None, fmt: str) -> str:
    """The default file name: `audit-<team|org>-<from|start>-<to|now>.<fmt>`, with every
    character a Windows or macOS file name could choke on (':' of a time) made '-'."""
    parts = [team or "org", start or "start", end or "now"]
    return "audit-" + "-".join(_SAFE.sub("-", part) for part in parts) + f".{fmt}"


_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def download_export(client, params: Mapping[str, str], target: Path) -> ExportReport:
    """Stream the export to `<target>.part`, flush it to disk, move it into place and
    verify it. Nothing is left behind when the download fails; an existing file is never
    overwritten."""
    if target.exists():
        raise CliError(
            f"{target} already exists",
            ExitCode.REFUSED,
            hint="pass another --output, or move the old export away",
        )
    partial = target.with_name(target.name + ".part")
    partial.unlink(missing_ok=True)  # our own leftover from an export that was cut off
    try:
        with partial.open("xb") as sink:
            client.export_audit(params, sink)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return verify_export(target)


def verify_exports(paths: Sequence[Path]) -> list[ExportReport]:
    reports = []
    for path in paths:
        try:
            reports.append(verify_export(path))
        except OSError as error:
            raise CliError(f"cannot read {path} ({error.strerror or error})") from None
    return reports
