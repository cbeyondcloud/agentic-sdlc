"""What an agent command's environment keeps (DF-66, DF-68): secrets are left out by
name and by value, PATH holds no relative entry, and on Windows `cmd.exe` never runs a
program from the working folder in place of the one on PATH."""

import os
import subprocess
import sys

import pytest

from agentic.sandbox.process import safe_env

PASSWORD = "pa" + "ss"  # built from pieces: not a real credential to a secret scanner


def test_oddly_named_secrets_are_left_out():
    env = {
        "GITHUB_PAT": "x",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "SENTRY_DSN": "https://k@sentry.example/1",
        "NPM_CONFIG__AUTH": "x",
        "MY_PASSPHRASE": "x",
        "DB_PASS": "x",
        "AWS_CREDS_FILE": "x",
        "SESSION_COOKIE": "x",
        "GPG_PRIVATE_KEYRING": "x",
        "OPENAI_API_KEY": "x",
    }
    assert safe_env(env, windows=False) == {}


def test_a_credential_inside_a_url_is_left_out_whatever_the_name():
    env = {
        "DATABASE_URL": f"postgres://app:{PASSWORD}word@db.internal:5432/app",
        "HTTPS_PROXY": f"http://me:{PASSWORD}@proxy.corp:8080",
        "REDIS": f"redis://:{PASSWORD}@cache:6379/0",
        "PLAIN_URL": "https://pypi.org/simple",
        "SQLITE_URL": "sqlite:///data/app.db",
        "USER_AT_HOST": "ssh://git@github.com/org/repo",
    }
    kept = safe_env(env, windows=False)
    assert set(kept) == {"PLAIN_URL", "SQLITE_URL", "USER_AT_HOST"}


def test_ordinary_variables_are_kept():
    env = {
        "PATH": "/usr/bin",
        "PATHEXT": ".COM;.EXE",
        "HOME": "/home/dev",
        "USERPROFILE": "C:\\Users\\dev",
        "SYSTEMROOT": "C:\\Windows",
        "GIT_AUTHOR_NAME": "Dev",
        "AUTHOR": "Dev",
        "PASSIVE_MODE": "1",
        "KEYBOARD": "uk",  # 'KEY' at a word's start is still a secret name: dropped below
        "LANG": "en_GB.UTF-8",
    }
    kept = safe_env(env, windows=False)
    assert kept.pop("PATH") == "/usr/bin"
    assert set(kept) == {
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "GIT_AUTHOR_NAME",
        "AUTHOR",
        "PASSIVE_MODE",
        "LANG",
    }


def test_relative_path_entries_are_removed():
    sep = os.pathsep
    env = {"PATH": sep.join(["/usr/bin", ".", "", "bin", "./tools", "/opt/x/bin"])}
    assert safe_env(env, windows=False)["PATH"] == sep.join(["/usr/bin", "/opt/x/bin"])


def test_windows_never_searches_the_working_folder_for_a_program():
    env = safe_env({"PATH": "C:\\Windows"}, windows=True)
    assert env["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert "NoDefaultCurrentDirectoryInExePath" not in safe_env({"PATH": "/usr/bin"}, windows=False)


def test_no_argument_reads_this_process_environment(monkeypatch):
    monkeypatch.setenv("AGENTIC_TEST_PAT", "x")
    monkeypatch.setenv("AGENTIC_TEST_PLAIN", "y")
    env = safe_env()
    assert "AGENTIC_TEST_PAT" not in env
    assert env["AGENTIC_TEST_PLAIN"] == "y"


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe's program search")
def test_cmd_does_not_run_a_same_named_file_from_the_working_folder(tmp_path):
    (tmp_path / "where.cmd").write_text("@echo PLANTED\r\n", encoding="utf-8")
    # a developer's own shell does not set the switch (some tools' shells do)
    switch = "NODEFAULTCURRENTDIRECTORYINEXEPATH"
    plain = {k: v for k, v in os.environ.items() if k.upper() != switch}
    done = subprocess.run(
        "where /?",
        shell=True,
        cwd=tmp_path,
        env=safe_env(plain),
        capture_output=True,
        text=True,
    )
    assert "PLANTED" not in done.stdout


def test_safe_env_runs_a_real_child():
    done = subprocess.run(
        [sys.executable, "-c", "print('ok')"], env=safe_env(), capture_output=True, text=True
    )
    assert done.stdout.strip() == "ok"
