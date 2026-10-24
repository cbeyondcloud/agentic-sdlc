"""Audit E2E (spec §14.5, M2-55): on a real (scripted) org run, `agentic audit verify` passes
for the run's local ledger and the control plane's audit chain; `agentic audit export`
writes exports (JSONL and CSV) that the standalone verifier verifies on its own; and a
tampered ledger entry and a tampered export row are found and located, with the documented
exit codes:

    agentic audit verify    0 intact, 3 a chain does not verify, 1 a file cannot be read
    auditproof.py           0 intact, 3 broken, 1 a file cannot be read, 64 wrong usage

What runs for real: the org stack of `_org_stack` (the merged control plane over HTTP on its
own database), the CLI in org mode (`login`, `init --repo`, `init`, `run`, `audit verify`,
`audit export`), the run's events ingested into the control plane, and
`core/src/agentic/auditproof.py` copied into an empty folder and run with `python -I`, as an
auditor would, importing nothing of ours. Stubbed as in M2-53: the models (a cassette),
GitHub's API, init's index build and the OS sandbox on Windows native."""

import csv
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

from _acceptance_support import HOST, MODELS, ROOT, Scenario, run_folder, sandbox_available, tool
from _invoice_story import PYTEST, REQUEST, design, passing, requirements
from _org_stack import PIPELINE, TEAM, org_mode, org_policy_repo, sign_in

from agentic_cli.exit_codes import ExitCode

SCENARIO = "m2-55"
DEVELOPER = "dev@example.invalid"
AUDITOR = "auditor@acme.example"
SCRIPT = {
    "requirements": [requirements()],
    "baseline": [tool("run_shell", command=PYTEST), passing("Baseline: 2 pass.", 2, ())],
    "design": [design()],
}
VERIFIER = ROOT / "core" / "src" / "agentic" / "auditproof.py"


def auditproof(folder: Path, *files: Path) -> subprocess.CompletedProcess:
    """The standalone verifier as an auditor runs it: one file copied anywhere, the
    standard library only (`-I`: no site packages, no environment, not even its folder)."""
    folder.mkdir(exist_ok=True)
    script = folder / "auditproof.py"
    if not script.exists():
        shutil.copyfile(VERIFIER, script)
    return subprocess.run(
        [sys.executable, "-I", str(script), *map(str, files)],
        cwd=folder,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def org_run(scenario: Scenario, org, tmp_path: Path) -> tuple[Path, str, str]:
    """The developer's org run, as in M2-53: (repo, run id, developer token)."""
    repo = scenario.existing_repo()
    policy = org_policy_repo(
        tmp_path / "policy-repo", MODELS, sandbox=sandbox_available(), hosts=f"{HOST}/acme/*"
    )
    assert org.connect_policy(policy)["kind"] == "activated"
    token = org.person(DEVELOPER, "developer", team=TEAM)
    org_mode(scenario, org)
    sign_in(scenario, token)
    setup = scenario.ok("--json", "init", "--repo", "--team", TEAM).json()
    scenario.merge_setup_pull_request(repo, setup["branch"])
    scenario.ok("--json", "init")
    scenario.models(SCRIPT)
    done = scenario.ok("--json", "run", PIPELINE, "--input", f"request={REQUEST}").json()
    assert done["status"] == "completed", done
    assert scenario.finish_models() == []
    return repo, done["run_id"], token


def change_one_field(line: bytes) -> tuple[bytes, str]:
    """`line` (a chained JSON entry) with one recorded value changed and its hash left as it
    was - an edit after the fact; returns the new line and the field changed."""
    entry = json.loads(line)
    name = next(
        key
        for key, value in entry.items()
        if isinstance(value, str) and key not in ("hash", "prev", "signature", "run_id")
    )
    entry[name] = entry[name] + " (edited)"
    return json.dumps(entry, ensure_ascii=False).encode("utf-8"), name


def test_audit_verify_and_export_on_an_org_run(scenario: Scenario, org, tmp_path):
    repo, run_id, token = org_run(scenario, org, tmp_path)
    events = run_folder(repo, run_id) / "events.jsonl"
    entries = [line for line in events.read_bytes().split(b"\n") if line.strip()]

    # the control plane holds the same chain the laptop wrote (ingest)
    held = org.ok("GET", f"/v1/runs/{run_id}", token)
    assert held["head"]["hash"] == json.loads(entries[-1])["hash"]
    assert held["head"]["seq"] == len(entries)

    # ── an auditor signs in: the local ledger and the org's chain both verify ──────────
    sign_in(scenario, org.person(AUDITOR, "auditor"))
    verified = scenario.ok("--json", "audit", "verify", run_id, "--control-plane").json()
    assert verified["intact"] is True, verified
    (run,) = verified["runs"]
    assert (run["run_id"], run["intact"], run["entries"]) == (run_id, True, len(entries))
    chain = verified["control_plane"]
    assert (chain["tenant"], chain["intact"]) == ("acme", True)
    records = org.audit()
    assert chain["records"] == len(records)
    assert chain["head"] == {"seq": records[-1]["seq"], "hash": records[-1]["hash"]}
    assert {"tenant.created", "policy.activated", "token.issued"} <= {r["event"] for r in records}

    # ── exports, verified by the CLI and by the standalone verifier ────────────────────
    jsonl, table = tmp_path / "org.jsonl", tmp_path / "org.csv"
    exported = scenario.ok("--json", "audit", "export", "-o", str(jsonl)).json()
    assert (exported["intact"], exported["entries"]) == (True, len(records))
    exported = scenario.ok("--json", "audit", "export", "--format", "csv", "-o", str(table))
    assert exported.json()["intact"] is True
    auditor = tmp_path / "auditor"
    checked = auditproof(auditor, jsonl, table)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert checked.stdout.count(": intact - ") == 2

    # ── a ledger entry edited after the fact: found, located, exit 3 ───────────────────
    edited, name = change_one_field(entries[1])
    events.write_bytes(b"\n".join([entries[0], edited, *entries[2:]]) + b"\n")
    broken = scenario.ok("--json", "audit", "verify", run_id, code=ExitCode.INVALID).json()
    (run,) = broken["runs"]
    assert run["intact"] is False and run["problems"], run  # Zyroflo's verdict, too
    (finding,) = run["findings"]
    assert (finding["file"], finding["entry"], finding["seq"], finding["field_class"]) == (
        "events.jsonl",
        2,
        2,
        "content",
    ), (finding, name)
    human = scenario.ok("audit", "verify", run_id, code=ExitCode.INVALID).out
    assert "events.jsonl entry 2 (seq 2): content" in human

    # ── an export row edited: found by the verifier and by the CLI, exit 3 ─────────────
    rows = jsonl.read_text(encoding="utf-8").splitlines()
    at = next(i for i, row in enumerate(rows) if '"policy.activated"' in row)
    row = json.loads(rows[at])
    seq = row["record"]["seq"]
    row["record"]["actor"] = "someone.else@acme.example"
    tampered = tmp_path / "org-tampered.jsonl"
    tampered.write_text("\n".join([*rows[:at], json.dumps(row), *rows[at + 1 :]]) + "\n", "utf-8")
    checked = auditproof(auditor, tampered)
    assert checked.returncode == 3, checked.stdout
    assert f"seq {seq}: content" in checked.stdout
    shown = scenario.ok("--json", "audit", "verify", "--export", str(tampered), code=3).json()
    (report,) = shown["exports"]
    assert [(f["seq"], f["field_class"]) for f in report["findings"]] == [(seq, "content")]

    # the CSV export edited the same way
    cells = list(csv.reader(io.StringIO(table.read_text(encoding="utf-8"), newline="")))
    columns = cells[0]
    at = next(i for i, c in enumerate(cells) if c[columns.index("event")] == "policy.activated")
    cells[at][columns.index("actor")] = "someone.else@acme.example"
    tampered_csv = tmp_path / "org-tampered.csv"
    with tampered_csv.open("w", encoding="utf-8", newline="") as out:
        csv.writer(out).writerows(cells)
    checked = auditproof(auditor, tampered_csv)
    assert checked.returncode == 3 and f"seq {seq}: content" in checked.stdout, checked.stdout

    # an export cut short (no proof line), a missing file, no file at all
    cut = tmp_path / "org-cut.jsonl"
    cut.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")
    assert auditproof(auditor, cut).returncode == 3
    assert auditproof(auditor, tmp_path / "missing.jsonl").returncode == 1
    assert auditproof(auditor).returncode == 64
    missing = scenario.cli("--json", "audit", "verify", "--export", str(tmp_path / "gone.jsonl"))
    assert missing.code == ExitCode.ERROR
