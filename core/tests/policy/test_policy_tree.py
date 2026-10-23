"""The policy repo's definitions checked as a whole (M2-15, M2-23): what both pull request
gates run - `agentic policy check` and the control plane's policy sync - so a broken
overlay, rollout or pin is caught before any laptop loads it."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import load_policy
from agentic.policy.tree import definition_problems

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "policy" / "policy"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def agent(name: str, version: str) -> str:
    return f"---\nname: {name}\ntype: general\nversion: {version}\ndescription: d\n---\nGo.\n"


PIPELINE = (
    "name: deliver\nversion: {v}\nsteps:\n  - id: design\n    agent: developer\n"
    "  - id: build\n    agent: developer\n  - gate: ship\n    approver_role: approver\n"
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "org-policy"
    shutil.copytree(FIXTURE, root / "policy")
    write(root / "agents" / "developer.md", agent("developer", "1.3.0"))
    write(root / "agents" / "versions" / "developer" / "1.2.0.md", agent("developer", "1.2.0"))
    write(root / "pipelines" / "deliver.yaml", PIPELINE.format(v="1.0.0"))
    write(
        root / "teams" / "payments" / "pipelines" / "overlays" / "payments-api.yaml",
        "pipelines:\n  deliver:\n    remove: [design]\n",
    )
    return root


def problems(root: Path) -> list[str]:
    return definition_problems(root, load_policy(root / "policy"))


def append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def rollout(root: Path, version="1.3.0", previous="1.2.0") -> None:
    write(
        root / "policy" / "rollout.yaml",
        f"rollout:\n  agents:\n    developer: {{version: {version}, previous: {previous}, "
        "teams: [payments]}\n",
    )


def test_valid_tree_has_no_problems(root: Path):
    rollout(root)
    assert problems(root) == []


def test_gate_names_a_refused_overlay(root: Path):
    append(root / "policy" / "org.yaml", "required_steps:\n  deliver: [design]\n")
    found = problems(root)
    assert any(
        p.startswith("teams/payments/pipelines/overlays/payments-api.yaml: ")
        and "rule required-step" in p
        for p in found
    ), found


def test_overlay_of_unknown_pipeline_is_a_problem(root: Path):
    write(
        root / "teams" / "payments" / "pipelines" / "overlays" / "billing.yaml",
        "pipelines:\n  ghost:\n    remove: [design]\n",
    )
    found = problems(root)
    assert any("billing.yaml" in p and "no pipeline named 'ghost'" in p for p in found), found


def test_a_stray_overlay_file_is_a_problem(root: Path):
    write(root / "teams" / "payments" / "pipelines" / "overlays" / "billing.yml", "x: 1\n")
    assert any("unexpected entries" in p and "billing.yml" in p for p in problems(root))


def test_rollout_without_previous_archive_is_a_problem(root: Path):
    rollout(root, previous="1.1.0")
    found = problems(root)
    assert any("agent developer@1.1.0" in p and "versions/developer/1.1.0.md" in p for p in found)


def test_rollout_version_must_be_current(root: Path):
    rollout(root, version="1.4.0")
    found = problems(root)
    assert (
        "policy/rollout.yaml: rollout.agents.developer: version 1.4.0 is not the "
        "current agents/developer.md (1.3.0)" in found
    )


def test_rollout_of_unknown_definition_is_a_problem(root: Path):
    write(
        root / "policy" / "rollout.yaml",
        "rollout:\n  pipelines:\n    ghost: {version: 2.0.0, previous: 1.0.0, teams: []}\n",
    )
    assert "policy/rollout.yaml: rollout.pipelines.ghost: no org pipeline named 'ghost'" in (
        problems(root)
    )


def test_pin_of_unknown_version_is_a_problem(root: Path):
    append(root / "policy" / "teams" / "payments.yaml", "pins:\n  agents: {developer: 0.9.0}\n")
    found = problems(root)
    assert any("agent developer@0.9.0" in p for p in found), found


def test_a_broken_archive_is_a_problem(root: Path):
    write(root / "agents" / "versions" / "developer" / "1.1.0.md", agent("developer", "1.0.0"))
    write(root / "agents" / "versions" / "developer" / "notes.txt", "x")
    write(root / "pipelines" / "versions" / "stray.yaml", "x: 1\n")
    found = problems(root)
    assert any("agents/versions/developer/1.1.0.md" in p and "1.0.0" in p for p in found), found
    assert any("agents/versions/developer/notes.txt" in p for p in found), found
    assert any("pipelines/versions/stray.yaml" in p for p in found), found


def test_each_problem_is_reported_once(root: Path):
    write(root / "agents" / "broken.md", "---\nname: broken\n---\n")
    found = problems(root)
    assert len([p for p in found if p.startswith("agents/broken.md")]) == len(
        set(p for p in found if p.startswith("agents/broken.md"))
    )
    assert found
