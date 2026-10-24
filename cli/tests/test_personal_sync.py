"""Personal skill and note backup on the laptop (M2-20; Review Focus 5): each change to
`~/.agentic/skills` or `~/.agentic/memory` becomes one chained journal event, the publish
scan keeps a file holding a secret or personal data on the laptop (only its digest and
finding counts are journaled), the journal and a durable cursor are the offline queue -
events queued offline replay once and in order - and `restore` brings the newest copies
back without ever writing outside the user's skills and notes."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic import auth
from agentic.orgapi import PersonalFile
from agentic.security.pii import PiiDetector
from agentic.security.publish_scan import PublishGuard
from agentic.security.secret_scan import SecretScanner
from agentic_cli import control_plane, personal_sync
from agentic_cli.config import agentic_home
from agentic_cli.control_plane import CONTROL_PLANE_TOKEN, ControlPlaneClient, record_login
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main
from agentic_cli.personal_sync import deliver, journal_entries, record_changes, restore
from sdlc_commons.ledger import entry_hash

EMAIL = "jane" + "@" + "example.com"
SKILL = "---\nname: review-helper\ndescription: Review checklist.\n---\nCheck tests first.\n"


@pytest.fixture
def agentic(home) -> Path:
    root = agentic_home()
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def guard() -> PublishGuard:
    return PublishGuard(SecretScanner(), PiiDetector(), on_event=lambda event: None)


@pytest.fixture
def fake() -> FakeControlPlane:
    return FakeControlPlane()


def write(root: Path, rel: str, text: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_bytes(text.encode("utf-8"))
    return path


def client(fake) -> ControlPlaneClient:
    return ControlPlaneClient(CP_URL, TOKEN, transport=fake.transport)


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_a_change_is_journaled_once(agentic, guard):
    write(agentic, "skills/review-helper/SKILL.md", SKILL)
    write(agentic, "memory/notes.md", "use uv\r\n")  # CRLF kept: a restore gives these bytes
    first = record_changes(agentic, guard)
    assert first.saved == ["memory/notes.md", "skills/review-helper/SKILL.md"]
    assert record_changes(agentic, guard).saved == []  # nothing changed
    write(agentic, "memory/notes.md", "use uv, always\n")
    assert record_changes(agentic, guard).saved == ["memory/notes.md"]
    entries = journal_entries(agentic)
    assert [e["event"] for e in entries] == ["personal.saved"] * 3
    assert entries[0]["content"] == "use uv\r\n"
    assert entries[0]["sha256"] == sha(b"use uv\r\n")
    assert entries[1]["kind"] == "skill" and entries[0]["kind"] == "memory"


def test_the_journal_is_one_chain(agentic, guard):
    write(agentic, "memory/a.md", "a")
    record_changes(agentic, guard)
    write(agentic, "memory/b.md", "b")
    record_changes(agentic, guard)
    prev = ""
    for seq, entry in enumerate(journal_entries(agentic), 1):
        assert entry["seq"] == seq and entry["prev"] == prev
        assert entry["hash"] == entry_hash(entry)
        prev = entry["hash"]


def test_a_removal_is_journaled(agentic, guard):
    note = write(agentic, "memory/old.md", "old")
    record_changes(agentic, guard)
    note.unlink()
    assert record_changes(agentic, guard).removed == ["memory/old.md"]
    last = journal_entries(agentic)[-1]
    assert last["event"] == "personal.removed" and "content" not in last


def test_secret_file_is_held_back(agentic, guard):
    write(agentic, "memory/contacts.md", f"ask {EMAIL}")
    recorded = record_changes(agentic, guard)
    assert recorded.held == ["memory/contacts.md"] and recorded.saved == []
    (entry,) = journal_entries(agentic)
    assert entry["event"] == "personal.held" and "content" not in entry
    assert entry["findings"] == {"pii:email": 1}
    journal = (agentic / "sync" / "personal" / "journal.jsonl").read_text(encoding="utf-8")
    assert EMAIL not in journal
    assert record_changes(agentic, guard).held == []  # not held again until it changes
    write(agentic, "memory/contacts.md", "ask the team channel")
    assert record_changes(agentic, guard).saved == ["memory/contacts.md"]


def test_what_cannot_be_backed_up_is_skipped_and_named(agentic, guard):
    write(agentic, "skills/tool/run.sh", "echo hi\n")
    write(agentic, "memory/binary.md", b"\xff\xfe\x00bad")
    write(agentic, "memory/huge.md", "x" * (personal_sync.MAX_CONTENT_BYTES + 1))
    write(agentic, f"memory/{EMAIL}.md", "named after a person")
    write(agentic, "memory/.DS_Store", "junk")
    recorded = record_changes(agentic, guard)
    assert recorded.saved == [] and journal_entries(agentic) == []
    skipped = dict(recorded.skipped)
    assert set(skipped) == {
        "skills/tool/run.sh",
        "memory/binary.md",
        "memory/huge.md",
        f"memory/{EMAIL}.md",
    }
    assert EMAIL not in json.dumps(journal_entries(agentic))


def test_offline_events_replay_once_in_order(agentic, guard, fake):
    for n in range(3):
        write(agentic, f"memory/n{n}.md", f"note {n}")
        record_changes(agentic, guard)
    fake.down = True
    offline = deliver(agentic, client(fake))
    assert (offline.sent, offline.queued) == (0, 3) and offline.error
    fake.down = False
    online = deliver(agentic, client(fake))
    assert (online.sent, online.queued, online.error) == (3, 0, None)
    assert [e["path"] for e in fake.personal()] == ["memory/n0.md", "memory/n1.md", "memory/n2.md"]
    assert deliver(agentic, client(fake)).sent == 0  # nothing twice
    # a lost cursor re-delivers from the start; the control plane takes replays as no-ops
    for cursor in (agentic / "sync" / "personal" / "cursors").iterdir():
        cursor.unlink()
    assert deliver(agentic, client(fake)).sent == 3
    assert len(fake.personal()) == 3


def test_each_control_plane_and_user_has_its_own_cursor(agentic, guard, fake):
    write(agentic, "memory/a.md", "a")
    record_changes(agentic, guard)
    assert deliver(agentic, client(fake), scope="one").sent == 1
    other = FakeControlPlane()
    assert deliver(agentic, client(other), scope="two").sent == 1


def test_two_processes_share_the_journal(agentic, guard):
    for n in range(20):
        write(agentic, f"memory/n{n}.md", f"note {n}")
    errors = []

    def backup():
        try:
            record_changes(agentic, guard)
        except Exception as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=backup) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    entries = journal_entries(agentic)
    assert sorted(e["path"] for e in entries) == sorted(f"memory/n{n}.md" for n in range(20))
    assert [e["seq"] for e in entries] == list(range(1, 21))


def _file(path: str, content: str | None, *, kind="memory", deleted=False) -> PersonalFile:
    data = (content or "").encode("utf-8")
    return PersonalFile(
        kind=kind,
        path=path,
        sha256=sha(data),
        content=content,
        deleted=deleted,
        device="0" * 32,
        seq=1,
        at="2026-10-24T10:00:00+00:00",
    )


def test_restore_writes_missing_files_and_keeps_changed_ones(agentic, guard):
    write(agentic, "memory/mine.md", "edited here")
    files = [
        _file("memory/mine.md", "from the backup"),
        _file("memory/new.md", "restored\r\n"),
        _file("skills/review-helper/SKILL.md", SKILL, kind="skill"),
        _file("memory/gone.md", None, deleted=True),
    ]
    done = restore(agentic, files)
    assert done.written == ["memory/new.md", "skills/review-helper/SKILL.md"]
    assert done.kept == ["memory/mine.md"]
    assert (agentic / "memory" / "new.md").read_bytes() == b"restored\r\n"
    assert not (agentic / "memory" / "gone.md").exists()
    assert (agentic / "memory" / "mine.md").read_text(encoding="utf-8") == "edited here"
    forced = restore(agentic, files, force=True)
    assert forced.written == ["memory/mine.md"] and sorted(forced.unchanged) == [
        "memory/new.md",
        "skills/review-helper/SKILL.md",
    ]
    # restored files are already backed up: the next backup sends nothing for them
    assert record_changes(agentic, guard).saved == []


def test_restore_refuses_escaping_paths(agentic, tmp_path):
    outside = tmp_path / "outside.md"
    bad = [
        _file("../outside.md", "x"),
        _file("memory/../../outside.md", "x"),
        _file("skills/x/run.sh", "x", kind="skill"),
        _file("memory/a.md", "x").model_copy(update={"sha256": sha(b"other")}),
    ]
    done = restore(agentic, bad, force=True)
    assert done.written == [] and len(done.refused) == 4
    assert not outside.exists() and not (agentic / "memory" / "a.md").exists()


def test_restore_refuses_a_linked_folder(agentic, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    try:
        (agentic / "memory").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot make symbolic links")
    done = restore(agentic, [_file("memory/a.md", "x")])
    assert done.written == [] and done.refused
    assert list(target.iterdir()) == []


# ── the commands ─────────────────────────────────────────────────────────────


@pytest.fixture
def org(home, repo, monkeypatch, fake):
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    for name in ("AGENTIC_CONTROL_PLANE_URL", "AGENTIC_CONTROL_PLANE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    config = agentic_home()
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        f"org: acme\ncontrol_plane_url: {CP_URL}\nteam: payments\n", encoding="utf-8"
    )
    auth.Credentials().store(CONTROL_PLANE_TOKEN, TOKEN)
    record_login(config, CP_URL, "acme", "dev@acme.example")
    monkeypatch.chdir(repo)
    return config


def test_skills_backup_and_restore_commands(org, fake, capsys):
    write(org, "memory/notes.md", "use uv")
    write(org, "memory/contacts.md", f"ask {EMAIL}")
    assert main(["--json", "skills", "backup"]) == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert out["saved"] == ["memory/notes.md"] and out["held"] == ["memory/contacts.md"]
    assert (out["sent"], out["queued"]) == (2, 0)
    (org / "memory" / "notes.md").unlink()
    assert main(["--json", "skills", "restore"]) == ExitCode.OK
    restored = json.loads(capsys.readouterr().out)
    assert restored["written"] == ["memory/notes.md"]
    assert (org / "memory" / "notes.md").read_text(encoding="utf-8") == "use uv"


def test_skills_backup_offline_stays_queued(org, fake, capsys):
    write(org, "memory/notes.md", "use uv")
    fake.down = True
    assert main(["--json", "skills", "backup"]) == ExitCode.ERROR
    out = json.loads(capsys.readouterr().out)
    assert (out["sent"], out["queued"]) == (0, 1) and out["error"]
    fake.down = False
    assert main(["--json", "sync"]) == ExitCode.OK  # sync sends the queued backup too
    out = json.loads(capsys.readouterr().out)
    assert out["personal"] == {"sent": 1, "queued": 0}
    assert [e["path"] for e in fake.personal()] == ["memory/notes.md"]


def test_sync_of_a_missing_run_is_an_error(org, capsys):
    """DF-497: a mistyped run id used to print 'sent 0, queued 0' and exit 0."""
    assert main(["--json", "sync", "no-such-run"]) == ExitCode.INVALID
    assert "no-such-run" in json.loads(capsys.readouterr().err)["error"]
