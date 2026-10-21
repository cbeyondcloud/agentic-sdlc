"""Prompt drift in the acceptance cassettes is never silent (DF-316). A replay answers by
call order, and the requests carry per-run values (run ids, temporary paths, commit
ids), so request hashes cannot show drift. Each cassette instead records a fingerprint
of what the prompts are made of - the library agents and pipelines, the standalone
policy and the handoff templates; a different fingerprint at replay warns, and fails
when AGENTIC_ACCEPTANCE_STRICT=1."""

from pathlib import Path

import pytest
from _acceptance_support import CASSETTES, prompt_drift, prompt_fingerprint

SCENARIOS = ("m1-68", "m1-69", "m1-70")


def test_the_fingerprint_ignores_line_endings(tmp_path: Path):
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    for folder, text in ((lf, b"a\nb\n"), (crlf, b"a\r\nb\r\n")):
        folder.mkdir()
        (folder / "agent.md").write_bytes(text)
    assert prompt_fingerprint([lf], root=lf) == prompt_fingerprint([crlf], root=crlf)
    (crlf / "agent.md").write_bytes(b"a\r\nc\r\n")
    assert prompt_fingerprint([lf], root=lf) != prompt_fingerprint([crlf], root=crlf)


def test_the_same_fingerprint_is_no_problem(monkeypatch):
    monkeypatch.setenv("AGENTIC_ACCEPTANCE_STRICT", "1")
    assert prompt_drift("sha256:a", "sha256:a") == []


def test_a_changed_prompt_warns_and_names_the_recorder(monkeypatch):
    monkeypatch.delenv("AGENTIC_ACCEPTANCE_STRICT", raising=False)
    with pytest.warns(UserWarning, match="record_acceptance.py"):
        assert prompt_drift("sha256:a", "sha256:b") == []


def test_a_changed_prompt_fails_in_strict_mode(monkeypatch):
    monkeypatch.setenv("AGENTIC_ACCEPTANCE_STRICT", "1")
    [problem] = prompt_drift(None, "sha256:b")
    assert "record_acceptance.py" in problem


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_committed_cassettes_match_the_current_prompts(scenario: str):
    """The cassettes in the repository were recorded from the prompts in the repository
    (a warning when not; a failure with AGENTIC_ACCEPTANCE_STRICT=1)."""
    stamp = CASSETTES / scenario / "acceptance" / "prompts.sha256"
    recorded = stamp.read_text("utf-8").strip() if stamp.is_file() else None
    assert prompt_drift(recorded, prompt_fingerprint()) == []
