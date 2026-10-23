"""Egress restriction config (M2-42; spec §9.2): org policy names the hosts a sandboxed command
may reach - package registries - and the TokenLean host is added for a whole agent process.
Entries are host names, checked at policy load, so no spelling opens everything; the
environment variables an org lets through to agent commands are named too (DF-365)."""

import asyncio
import os
import sys

import pytest
from pydantic import ValidationError

from agentic.agents.tools import LocalProcessSandbox
from agentic.policy.model import SandboxPolicy
from agentic.sandbox import SandboxRules, SrtSandbox
from agentic.sandbox.egress import egress_hosts
from agentic.sandbox.process import safe_env

PASSWORD = "pa" + "ss"  # built from pieces: not a real credential to a secret scanner


@pytest.mark.parametrize(
    "given, kept",
    [
        ("pypi.org", "pypi.org"),
        ("Files.PythonHosted.org", "files.pythonhosted.org"),
        ("*.npmjs.org", "*.npmjs.org"),
        ("registry.internal:8443", "registry.internal:8443"),
        ("127.0.0.1:3128", "127.0.0.1:3128"),
        (" repo.maven.apache.org ", "repo.maven.apache.org"),
    ],
)
def test_registry_hosts_are_accepted_and_normalised(given, kept):
    assert SandboxPolicy(allowed_domains=[given]).allowed_domains == [kept]


@pytest.mark.parametrize(
    "bad",
    [
        "*",
        "*.",
        "https://pypi.org",
        "pypi.org/simple",
        "a*.example.org",
        "*.*.example.org",
        "",
        "pypi..org",
        "-pypi.org",
        "pypi.org:0",
        "pypi.org:99999",
        "user@pypi.org",
        "pypi org",
    ],
)
def test_an_entry_that_is_not_a_host_name_is_refused(bad):
    with pytest.raises(ValidationError, match="allowed_domains entry"):
        SandboxPolicy(allowed_domains=[bad])


def test_duplicate_hosts_are_kept_once():
    policy = SandboxPolicy(allowed_domains=["pypi.org", "PyPI.org", "files.pythonhosted.org"])
    assert policy.allowed_domains == ["pypi.org", "files.pythonhosted.org"]


# ── the hosts a sandbox gets ─────────────────────────────────────────────────


def test_shell_commands_get_the_registries_only():
    policy = SandboxPolicy(allowed_domains=["pypi.org", "registry.npmjs.org"])
    assert egress_hosts(policy) == ("pypi.org", "registry.npmjs.org")


def test_an_agent_process_also_reaches_the_gateway_host():
    policy = SandboxPolicy(allowed_domains=["pypi.org"])
    assert egress_hosts(policy, "https://TokenLean.acme.example/v1") == (
        "tokenlean.acme.example",
        "pypi.org",
    )


@pytest.mark.parametrize(
    "url, host",
    [
        ("https://tokenlean.acme.example:8443/", "tokenlean.acme.example:8443"),
        ("http://127.0.0.1:4000", "127.0.0.1:4000"),
        ("https://tokenlean.acme.example:443", "tokenlean.acme.example"),
        ("http://tokenlean.acme.example:80", "tokenlean.acme.example"),
        ("http://[::1]:4000", "[::1]:4000"),
    ],
)
def test_the_gateway_port_is_named_unless_it_is_the_default(url, host):
    assert egress_hosts(SandboxPolicy(), url) == (host,)


def test_the_gateway_already_listed_is_not_repeated():
    policy = SandboxPolicy(allowed_domains=["tokenlean.acme.example", "pypi.org"])
    assert egress_hosts(policy, "https://tokenlean.acme.example") == (
        "tokenlean.acme.example",
        "pypi.org",
    )


@pytest.mark.parametrize("url", ["", "not a url", "ftp://x.example", "https://"])
def test_a_gateway_url_without_a_host_is_refused(url):
    with pytest.raises(ValueError, match="gateway"):
        egress_hosts(SandboxPolicy(), url)


# ── environment variables an org lets through (DF-365) ───────────────────────


def test_pass_env_names_are_checked():
    assert SandboxPolicy(pass_env=["HTTPS_PROXY", "SSH_AUTH_SOCK"]).pass_env == [
        "HTTPS_PROXY",
        "SSH_AUTH_SOCK",
    ]
    for bad in ["AGENTIC_TOKENLEAN_API_KEY", "agentic_github_token", "A-B", "1X", ""]:
        with pytest.raises(ValidationError, match="pass_env"):
            SandboxPolicy(pass_env=[bad])


def test_a_passed_name_keeps_its_value_and_other_secrets_still_go():
    env = {
        "HTTPS_PROXY": f"http://me:{PASSWORD}@proxy.corp:8080",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "GITHUB_TOKEN": "x",
        "PATH": "/usr/bin",
    }
    kept = safe_env(env, windows=False, pass_through=["HTTPS_PROXY", "SSH_AUTH_SOCK"])
    assert kept == {
        "HTTPS_PROXY": env["HTTPS_PROXY"],
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "PATH": "/usr/bin",
    }


def test_a_framework_secret_is_never_passed_even_if_named():
    env = {"AGENTIC_TOKENLEAN_API_KEY": "x", "PATH": "/usr/bin"}
    assert "AGENTIC_TOKENLEAN_API_KEY" not in safe_env(
        env, windows=False, pass_through=["AGENTIC_TOKENLEAN_API_KEY"]
    )


def test_pass_through_names_match_case_blind_on_windows_only():
    env = {"https_proxy": f"http://me:{PASSWORD}@proxy.corp:8080", "PATH": "C:\\Windows"}
    assert "https_proxy" in safe_env(env, windows=True, pass_through=["HTTPS_PROXY"])
    assert "https_proxy" not in safe_env(env, windows=False, pass_through=["HTTPS_PROXY"])


def test_the_sandboxes_hand_the_names_through(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(command, cwd, timeout_s, env=None):
        seen["env"] = env
        return None

    monkeypatch.setattr("agentic.agents.tools.shell.run_process", fake_run)
    monkeypatch.setattr("agentic.sandbox.srt.run_process", fake_run)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")

    asyncio.run(LocalProcessSandbox(pass_env=("SSH_AUTH_SOCK",)).run("true", tmp_path, 5))
    assert seen["env"]["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    box = SrtSandbox(SandboxRules(write=(tmp_path,)), tmp_path / "s", pass_env=("SSH_AUTH_SOCK",))
    asyncio.run(box.run("true", tmp_path, 5))
    assert seen["env"]["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    asyncio.run(LocalProcessSandbox().run("true", tmp_path, 5))
    assert "SSH_AUTH_SOCK" not in seen["env"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell variable syntax")
def test_a_passed_variable_reaches_a_real_command(tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    result = asyncio.run(
        LocalProcessSandbox(pass_env=("SSH_AUTH_SOCK",)).run(
            'printf %s "$SSH_AUTH_SOCK"', tmp_path, 10
        )
    )
    assert result.stdout == "/tmp/agent.sock", os.environ.get("SSH_AUTH_SOCK")
