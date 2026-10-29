"""What an agent command's environment keeps (DF-66, DF-68): secrets are left out by
name and by value, PATH holds no relative entry, and on Windows `cmd.exe` never runs a
program from the working folder in place of the one on PATH."""

import logging
import os
import subprocess
import sys
import time

import pytest

from agentic.sandbox import process
from agentic.sandbox.process import safe_env, secret_value, withheld_names

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


# ── M1 close-out (DF-333, DF-334, DF-336) ─────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["a" * 20_000, "a://" * 5_000, "x" + "a:" * 10_000, "a://" + "b:" * 10_000],
    ids=["scheme-chars", "schemes", "colons", "userinfo-colons"],
)
def test_credential_detection_is_linear_on_long_values(value):  # DF-333
    started = time.monotonic()
    assert not secret_value(value)
    assert time.monotonic() - started < 0.25


def test_credentials_are_still_found():
    token = "t" * 25
    assert secret_value(f"https://user:{PASSWORD}@host/x")
    assert secret_value(f"https://user:{PASSWORD}@word@host/x")  # an unencoded '@'
    assert secret_value(f"see https://{token}@github.com/org/repo")
    assert secret_value(f"DATABASE_URL=postgres://app:{PASSWORD}@db/app")
    assert not secret_value("ssh://git@github.com/org/repo")


def test_a_long_credential_is_still_found():  # review F4: no length lets one through
    long = "p" * 5_000
    assert secret_value(f"https://user:{long}@host/x")
    assert secret_value(f"https://{long}@host/x")  # a long token as the user part
    assert secret_value(f"https://{'u' * 300}:{PASSWORD}@host/x")  # a long user name


@pytest.mark.parametrize(
    "value",
    ["a://b:" + "c:" * 20_000, ("a://b:" + "c" * 50) * 2_000, "a://" + "u" * 40_000],
    ids=["password-colons", "many-urls", "long-user"],
)
def test_unbounded_credential_detection_stays_linear(value):  # review F4
    started = time.monotonic()
    assert not secret_value(value)
    assert time.monotonic() - started < 0.25


def test_a_quoted_absolute_path_entry_is_kept():  # DF-336
    folder = "C:\\Program Files\\Tool" if os.name == "nt" else "/opt/my tools/bin"
    other = "C:\\Windows" if os.name == "nt" else "/usr/bin"
    env = {"PATH": os.pathsep.join([f'"{folder}"', other, '"relative dir"'])}
    assert safe_env(env)["PATH"] == os.pathsep.join([f'"{folder}"', other])


def test_an_all_relative_path_becomes_a_system_default_never_empty():  # DF-336
    """An empty PATH means the current folder to some shells: the agent's own tree."""
    env = safe_env({"PATH": os.pathsep.join([".", "bin"]), "SystemRoot": "C:\\Windows"})
    entries = env["PATH"].split(os.pathsep)
    assert entries and all(entry for entry in entries)
    if os.name == "nt":
        assert "C:\\Windows\\System32" in entries
    else:
        assert entries == ["/usr/local/bin", "/usr/bin", "/bin"]


def test_withheld_names_are_reported_once_and_never_their_values(caplog):  # DF-334
    """A proxy with a password, or the SSH agent's socket, is withheld from agent
    commands; the developer must be able to see why their tool lost the network."""
    process._REPORTED.clear()
    env = {
        "HTTPS_PROXY": f"http://me:{PASSWORD}word@proxy.corp:8080",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "LANG": "en_GB.UTF-8",
    }
    with caplog.at_level(logging.INFO, logger="agentic.sandbox"):
        safe_env(env, windows=False)
        safe_env(env, windows=False)

    records = [r for r in caplog.records if r.name == "agentic.sandbox"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    assert "HTTPS_PROXY" in text and "SSH_AUTH_SOCK" in text and "LANG" not in text
    # the value's password and host never appear (the remedy names `sandbox.pass_env`)
    assert f"me:{PASSWORD}word" not in text and f"{PASSWORD}word" not in text
    assert "proxy.corp" not in text
    assert withheld_names(env) == ["HTTPS_PROXY", "SSH_AUTH_SOCK"]


def test_safe_env_runs_a_real_child():
    done = subprocess.run(
        [sys.executable, "-c", "print('ok')"], env=safe_env(), capture_output=True, text=True
    )
    assert done.stdout.strip() == "ok"


def test_the_withheld_warning_says_why_and_how_to_pass_one(caplog):  # DF-372 (c)
    process._REPORTED.clear()
    env = {"SSH_AUTH_SOCK": "/tmp/agent.sock"}
    with caplog.at_level(logging.INFO, logger="agentic.sandbox"):
        safe_env(env, windows=False)
    text = next(r.getMessage() for r in caplog.records if r.name == "agentic.sandbox")
    assert "on purpose" in text
    assert "sandbox.pass_env" in text
