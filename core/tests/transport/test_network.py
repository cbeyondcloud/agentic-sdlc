"""Tests for corporate networks: proxies and CA bundles (M1-14)."""

import sys
from pathlib import Path

import httpx
import pytest

from agentic.transport import CallIdentity, NetworkError, NetworkSettings, make_client

IDENTITY = CallIdentity(team="payments", user="u123", repo="github.com/acme/api",
                        pipeline="feature-delivery", run_id="r-net-1", step="build",
                        agent="developer@1.2.0", policy_version="git:" + "a" * 40)  # fmt: skip


@pytest.fixture(autouse=True)
def no_ambient_network_settings(monkeypatch):
    """The developer's own proxy and CA variables must not leak into these tests."""
    names = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE",
             "REQUESTS_CA_BUNDLE")  # fmt: skip
    for name in (*names, *(n.lower() for n in names)):
        monkeypatch.delenv(name, raising=False)


def test_a_custom_ca_bundle_is_trusted(https_server, ca_bundle):
    client = make_client(https_server, IDENTITY, network=NetworkSettings(ca_bundle=ca_bundle))

    assert client.get("/").text == "ok"


def test_without_the_ca_the_server_is_refused(https_server):
    with pytest.raises(httpx.ConnectError):
        make_client(https_server, IDENTITY).get("/")


def test_ssl_cert_file_from_the_environment_is_honoured(https_server, ca_bundle, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_bundle))

    assert make_client(https_server, IDENTITY).get("/").text == "ok"


@pytest.mark.skipif(
    sys.platform.startswith("linux"),
    reason="OpenSSL itself reads SSL_CERT_FILE when loading Linux system CAs",
)
def test_environment_ca_files_are_ignored_when_trust_env_is_off(https_server, ca_bundle,
                                                                monkeypatch):  # fmt: skip
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_bundle))

    with pytest.raises(httpx.ConnectError):
        make_client(https_server, IDENTITY, network=NetworkSettings(trust_env=False)).get("/")


def test_a_missing_ca_bundle_is_a_clear_error(tmp_path: Path):
    missing = tmp_path / "nope.pem"

    with pytest.raises(NetworkError, match="nope.pem"):
        make_client("https://tl.example", IDENTITY, network=NetworkSettings(ca_bundle=missing))


def test_https_proxy_is_used(https_server, ca_bundle, connect_proxy, monkeypatch):
    proxy, seen = connect_proxy
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    client = make_client(https_server, IDENTITY, network=NetworkSettings(ca_bundle=ca_bundle))

    assert client.get("/").text == "ok"
    assert seen == [https_server.removeprefix("https://")]


def test_no_proxy_bypasses_the_proxy(https_server, ca_bundle, connect_proxy, monkeypatch):
    proxy, seen = connect_proxy
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("NO_PROXY", "localhost")
    client = make_client(https_server, IDENTITY, network=NetworkSettings(ca_bundle=ca_bundle))

    assert client.get("/").text == "ok"
    assert seen == []


def test_trust_env_off_ignores_proxy_variables(https_server, ca_bundle, connect_proxy,
                                               monkeypatch):  # fmt: skip
    proxy, seen = connect_proxy
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    settings = NetworkSettings(ca_bundle=ca_bundle, trust_env=False)

    assert make_client(https_server, IDENTITY, network=settings).get("/").text == "ok"
    assert seen == []


def test_the_async_client_uses_the_same_settings(https_server, ca_bundle):
    import asyncio

    from agentic.transport import make_async_client

    async def call() -> str:
        settings = NetworkSettings(ca_bundle=ca_bundle)
        async with make_async_client(https_server, IDENTITY, network=settings) as client:
            return (await client.get("/")).text

    assert asyncio.run(call()) == "ok"


# ── SP3 review fixes (M7) ─────────────────────────────────────────────────────


def test_a_ca_bundle_that_is_not_pem_is_a_clear_error(tmp_path: Path):
    junk = tmp_path / "not-a-cert.pem"
    junk.write_bytes(b"\x30\x82\x01\x0a not pem")

    with pytest.raises(NetworkError, match="not-a-cert.pem"):
        make_client("https://tl.example", IDENTITY, network=NetworkSettings(ca_bundle=junk))


def test_an_environment_ca_file_that_is_missing_is_a_clear_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "gone.pem"))

    with pytest.raises(NetworkError, match="REQUESTS_CA_BUNDLE"):
        make_client("https://tl.example", IDENTITY)
