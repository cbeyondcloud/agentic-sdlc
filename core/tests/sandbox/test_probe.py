"""Tests for the sandbox probe (`python -m agentic.sandbox.probe`): it must notice when
commands are NOT sandboxed (DF-60, DF-61 are closed by people running it)."""

import os
import socket

import pytest

from agentic.sandbox import probe


def test_probe_catches_a_missing_sandbox(tmp_path, capsys):
    assert probe.main(["--unsandboxed", "--workdir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "write outside worktree" in out
    assert "did NOT" in out


def test_every_case_names_what_it_wants():
    assert {wanted for _, _, wanted, _ in probe.CASES} == {"allowed", "blocked"}
    assert len({name for name, *_ in probe.CASES}) == len(probe.CASES)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no Unix sockets here")
def test_the_unix_socket_listens_even_under_a_long_work_folder(tmp_path):
    """macOS takes Unix socket paths up to 104 bytes: a socket under a deep work folder
    could not bind, and the "unix socket to host" case then read as blocked whatever the
    sandbox did. The socket must listen, so only the sandbox can block the case."""
    workdir = tmp_path / ("deep" * 30)
    (workdir / "wt").mkdir(parents=True)
    server, path = probe.unix_listener(workdir)
    try:
        assert len(os.fsencode(path)) <= probe.SOCKET_PATH_MAX
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
    finally:
        server.close()
        probe.remove_listener(path, workdir)
    assert not path.exists()
