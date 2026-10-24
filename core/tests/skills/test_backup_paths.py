"""Which paths a personal backup may name (M2-20): the control plane stores and `agentic
skills restore` writes only these, so a restore never leaves `~/.agentic/skills` and
`~/.agentic/memory`, on any OS."""

import pytest

from agentic.skills.personal import backup_path_problem


@pytest.mark.parametrize(
    ("kind", "path"),
    [
        ("skill", "skills/review-helper/SKILL.md"),
        ("skill", "skills/review-helper/references/checklist.md"),
        ("memory", "memory/notes.md"),
        ("memory", "memory/projects/payments.txt"),
    ],
)
def test_plain_skill_and_note_paths_are_accepted(kind, path):
    assert backup_path_problem(kind, path) is None


@pytest.mark.parametrize(
    ("kind", "path"),
    [
        ("skill", "../.ssh/id_rsa"),
        ("skill", "skills/../../.ssh/config.md"),
        ("skill", "/etc/passwd.md"),
        ("skill", "C:/Users/x/notes.md"),
        ("skill", "skills\\x\\SKILL.md"),
        ("skill", "skills/x/run.sh"),
        ("skill", "skills/Bad_Name/SKILL.md"),
        ("skill", "skills/SKILL.md"),
        ("skill", "memory/notes.md"),
        ("memory", "skills/x/SKILL.md"),
        ("memory", "memory/.git/config.md"),
        ("memory", "memory/./notes.md"),
        ("memory", "memory//notes.md"),
        ("memory", "memory/notes.md."),
        ("memory", "memory/con:x.md"),
        ("memory", "memory/Thumbs.db"),
        ("memory", ""),
        ("other", "memory/notes.md"),
        ("memory", None),
    ],
)
def test_escaping_or_odd_paths_are_refused(kind, path):
    assert backup_path_problem(kind, path) is not None
