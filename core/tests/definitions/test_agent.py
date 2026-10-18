"""Tests for loading an agent definition (a Markdown file with YAML front matter)."""

from pathlib import Path

import pytest

from agentic.definitions import DefinitionError, load_agent


def write_agent(folder: Path, front_matter: str, body: str = "Do the job.") -> Path:
    """Create an agent file from a YAML header and a Markdown body."""
    path = folder / "agent.md"
    path.write_text(f"---\n{front_matter}\n---\n{body}\n", encoding="utf-8")
    return path


MINIMAL = "name: helper\nversion: 0.1.0\ndescription: A minimal agent.\ntype: general"


def test_loads_spec_example(fixtures: Path):
    agent = load_agent(fixtures / "agents" / "developer.md")

    assert agent.type == "developer"
    assert (agent.name, agent.tier, agent.backend, agent.produces) == (
        "developer",
        "balanced",
        "native",
        "dev-report",
    )
    assert agent.children.per_child.max_turns == 30
    assert agent.instructions.startswith("You are the Developer.")


def test_unknown_field_rejected(tmp_path: Path):
    path = write_agent(tmp_path, MINIMAL + "\ncolour: red")

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert "colour" in str(error.value)


def test_empty_body_rejected(tmp_path: Path):
    path = write_agent(tmp_path, MINIMAL, body="   ")

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert "instructions" in str(error.value)


def test_bad_backend_rejected(tmp_path: Path):
    path = write_agent(tmp_path, MINIMAL + "\nbackend: ftp")

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert "backend" in str(error.value)


def test_minimal_agent_inherits_from_its_type(tmp_path: Path):
    agent = load_agent(write_agent(tmp_path, MINIMAL))

    # None = "not set here: inherit from the agent type" (D35)
    assert agent.type == "general"
    assert agent.tier is None
    assert agent.backend is None
    assert agent.tools is None
    assert agent.produces is None
    assert agent.limits.budget_usd is None
    assert agent.children.max_total is None


def test_type_is_required(tmp_path: Path):
    path = write_agent(tmp_path, "name: helper\nversion: 0.1.0\ndescription: x")

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert "type: Field required" in str(error.value)


def test_type_must_be_a_simple_name(tmp_path: Path):
    path = write_agent(tmp_path, MINIMAL.replace("type: general", "type: Dev Agent!"))

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert "type" in str(error.value)


def test_broken_yaml_reported_as_definition_error(tmp_path: Path):
    path = write_agent(tmp_path, "name: [unclosed")

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert str(path) in str(error.value)


# --- deny rules: what this agent may never do (D31) ---


def test_deny_rules_loaded(fixtures: Path):
    agent = load_agent(fixtures / "agents" / "developer.md")

    assert agent.deny.shell == ["git push", "git commit --amend"]
    assert agent.deny.paths == [".github/**", "infra/**"]


def test_deny_defaults_to_empty(tmp_path: Path):
    agent = load_agent(write_agent(tmp_path, MINIMAL))

    assert agent.deny.shell == []
    assert agent.deny.paths == []


def test_command_both_allowed_and_denied_rejected(tmp_path: Path):
    path = write_agent(tmp_path, MINIMAL + "\nshell: [pytest]\ndeny:\n  shell: [pytest]")

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert "pytest" in str(error.value)
    assert "both allowed and denied" in str(error.value)
    assert "- agent: shell commands are both allowed and denied: pytest" in str(error.value)


def test_unknown_deny_field_rejected(tmp_path: Path):
    path = write_agent(tmp_path, MINIMAL + "\ndeny:\n  network: [example.com]")

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert "deny.network" in str(error.value)


@pytest.mark.parametrize(
    "backend", ["native", "claude-code", "codex", "opencode", "acp:antigravity", "remote:devin"]
)
def test_supported_backend_names_accepted(tmp_path: Path, backend: str):
    agent = load_agent(write_agent(tmp_path, MINIMAL + f"\nbackend: {backend}"))

    assert agent.backend == backend


# --- reading files safely (SP1 review fixes, M1-74) ---


def test_repeated_deny_block_rejected(tmp_path: Path):
    # A second `deny:` used to silently replace the first, loosening the guardrail.
    path = write_agent(tmp_path, MINIMAL + "\ndeny:\n  shell: [kubectl]\ndeny:\n  shell: []")

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert "duplicate" in str(error.value).lower()


def test_byte_order_mark_is_ignored(tmp_path: Path):
    path = tmp_path / "agent.md"
    path.write_bytes(("﻿---\n" + MINIMAL + "\n---\nDo the job.\n").encode())

    assert load_agent(path).name == "helper"


def test_non_utf8_file_reported(tmp_path: Path):
    path = tmp_path / "agent.md"
    path.write_bytes(b"---\nname: caf\xe9\nversion: 0.1.0\ndescription: x\n---\nDo it.\n")

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert "UTF-8" in str(error.value)


def test_field_error_names_the_line(tmp_path: Path):
    # Front matter starts on line 2: name=2, version=3, description=4, type=5, temperature=6.
    path = write_agent(tmp_path, MINIMAL + "\ntemperature: 5")

    with pytest.raises(DefinitionError) as error:
        load_agent(path)

    assert "temperature: Input should be less than or equal to 2 (line 6)" in str(error.value)
