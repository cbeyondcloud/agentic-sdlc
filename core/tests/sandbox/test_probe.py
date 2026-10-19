"""Tests for the sandbox probe (`python -m agentic.sandbox.probe`): it must notice when
commands are NOT sandboxed (DF-60, DF-61 are closed by people running it)."""

from agentic.sandbox import probe


def test_probe_catches_a_missing_sandbox(tmp_path, capsys):
    assert probe.main(["--unsandboxed", "--workdir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "write outside worktree" in out
    assert "did NOT" in out


def test_every_case_names_what_it_wants():
    assert {wanted for _, _, wanted, _ in probe.CASES} == {"allowed", "blocked"}
    assert len({name for name, *_ in probe.CASES}) == len(probe.CASES)
