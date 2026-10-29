"""Personal learnings applied and reverted through Zyroflo's learning log (M3-16, DF-728):
the applied row is written before the file, Zyroflo's `undo` restores the prior bytes and
appends the reversal, and a file the learning created is removed again."""

import json
from pathlib import Path

import pytest
from zyroflo.sdlc import learning as zyroflo_learning

from sdlc_commons.learning import LearningApplyError, PersonalChanges

PID = "lp-" + "4" * 20
NOTE = "memory/learned-developer.md"


def test_personal_apply_and_revert_restore_bytes(tmp_path: Path):
    note = tmp_path / NOTE
    note.parent.mkdir(parents=True)
    note.write_bytes(b"# Mine\n\nKeep this.\n")
    changes = PersonalChanges(tmp_path)
    row = changes.apply(
        PID, NOTE, "# Mine\n\nKeep this.\n\n- learned\n", by="alice", why="3 corrections"
    )
    assert row["action"] == "applied" and row["path"] == NOTE
    assert note.read_bytes() == b"# Mine\n\nKeep this.\n\n- learned\n"
    assert changes.standing(PID)["proposal"] == PID
    undone = changes.revert(PID, by="alice")
    assert undone[0]["action"] == "undone" and not undone[0]["drifted"]
    assert note.read_bytes() == b"# Mine\n\nKeep this.\n"
    assert changes.standing(PID) is None


def test_created_file_is_removed_on_revert(tmp_path: Path):
    changes = PersonalChanges(tmp_path)
    skill = "skills/learned-qa/SKILL.md"
    changes.apply(PID, skill, "---\nname: learned-qa\n---\n", by="alice", why="w")
    case = "eval/cases/scoping/learned-x.yaml"
    changes.apply(f"{PID}#eval", case, "id: learned-x\n", by="alice", why="w")
    assert (tmp_path / skill).is_file() and (tmp_path / case).is_file()
    rows = changes.revert(PID, by="alice")  # the learning and its eval case
    assert {r["proposal"] for r in rows} == {PID, f"{PID}#eval"}
    assert not (tmp_path / skill).exists() and not (tmp_path / skill).parent.exists()
    assert not (tmp_path / case).exists()


def test_apply_twice_is_refused(tmp_path: Path):
    changes = PersonalChanges(tmp_path)
    changes.apply(PID, NOTE, "a\n", by="alice", why="w")
    with pytest.raises(LearningApplyError, match="already applied"):
        changes.apply(PID, NOTE, "b\n", by="alice", why="w")
    with pytest.raises(LearningApplyError, match="not applied"):
        changes.revert("lp-" + "5" * 20, by="alice")
    with pytest.raises(LearningApplyError, match="inside"):
        changes.apply("lp-" + "6" * 20, "../escape.md", "x\n", by="alice", why="w")


def test_crlf_kept_through_apply_and_revert(tmp_path: Path):
    note = tmp_path / NOTE
    note.parent.mkdir(parents=True)
    note.write_bytes(b"# Mine\r\n")
    changes = PersonalChanges(tmp_path)
    changes.apply(PID, NOTE, "# Mine\r\n\r\n- learned\r\n", by="alice", why="w")
    assert note.read_bytes() == b"# Mine\r\n\r\n- learned\r\n"
    changes.revert(PID, by="alice")
    assert note.read_bytes() == b"# Mine\r\n"


def test_rows_are_zyroflos_learning_log(tmp_path: Path):
    changes = PersonalChanges(tmp_path)
    changes.apply(PID, NOTE, "x\n", by="alice", why="w")
    changes.revert(PID, by="alice")
    rows = zyroflo_learning.history(tmp_path)
    assert [r["action"] for r in rows] == ["applied", "undone"]
    log = tmp_path / zyroflo_learning.LOG
    assert [json.loads(line)["proposal"] for line in log.read_text("utf-8").splitlines()] == [
        PID,
        PID,
    ]
    assert changes.history() == rows
