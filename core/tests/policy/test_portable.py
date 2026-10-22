"""`agentic.policy.portable` (review F4): the names a policy tree may hold, so Windows,
macOS and Linux laptops all extract the tree the control plane validated."""

import unicodedata

import pytest

from agentic.policy.portable import MAX_PATH_CHARS, portable_path_problems


def test_ordinary_policy_paths_are_portable():
    paths = [
        "policy/org.yaml",
        "policy/teams/payments.yaml",
        "teams/payments/agents/reviewer.md",
        "skills/addyosmani/api-and-interface-design/SKILL.md",
        ".github/CODEOWNERS",
        "docs/consolidated.md",  # holds "con", but is not the device name
    ]
    assert portable_path_problems(paths) == []


@pytest.mark.parametrize(
    "path",
    [
        "docs/a:b.txt",
        "docs/what?.md",
        "docs/a\\b.md",
        "docs/notes.",
        "docs/notes ",
        "docs/aux.md",
        "CON",
        "teams/LPT1/x.md",
        "docs/" + "x" * MAX_PATH_CHARS,
        unicodedata.normalize("NFD", "docs/café.md"),
    ],
)
def test_a_name_some_laptop_refuses_or_mangles_is_named(path):
    problems = portable_path_problems([path])
    assert len(problems) == 1 and problems[0].startswith(f"{path}:")
    assert "policy.path" in problems[0]


@pytest.mark.parametrize(
    "paths",
    [
        ["docs/Readme.md", "docs/README.md"],
        ["Docs/x.md", "docs/y.md"],
    ],
)
def test_names_differing_only_in_case_collide(paths):
    problems = portable_path_problems(paths)
    assert len(problems) == 1 and problems[0].startswith(paths[1])
    assert "collides" in problems[0]
