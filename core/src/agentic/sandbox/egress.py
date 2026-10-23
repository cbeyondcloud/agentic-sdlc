"""Which hosts a sandbox may reach (M2-42; spec §9.2, D38).

Org policy names the package registries (`sandbox.allowed_domains`, checked as host names
when the policy loads). An agent's shell commands get exactly those. A whole agent CLI
process run under srt (claude-code, codex, opencode) also calls the model, so it gets the
TokenLean host as well - and nothing else: every other host is refused by srt's proxy.
"""

import ipaddress

import httpx2

from agentic.policy.model import SandboxPolicy

_DEFAULT_PORTS = {"https": 443, "http": 80}


def egress_hosts(policy: SandboxPolicy, gateway_url: str | None = None) -> tuple[str, ...]:
    """The hosts a sandbox allows: the gateway's (for an agent process; with its port
    when that is not the scheme's default) first, then the registries, each once."""
    hosts = [gateway_host(gateway_url)] if gateway_url is not None else []
    return tuple(dict.fromkeys([*hosts, *policy.allowed_domains]))


def gateway_host(url: str) -> str:
    """`host[:port]` of the gateway URL, as srt's allowedDomains names it."""
    try:
        parsed = httpx2.URL(str(url).strip())
    except Exception:  # noqa: BLE001 - any unparsable URL is the same refusal
        parsed = None
    if parsed is None or parsed.scheme not in _DEFAULT_PORTS or not parsed.host:
        raise ValueError(f"the gateway URL {url!r} names no http(s) host to allow")
    host = parsed.host.lower()
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass
    port = parsed.port
    return host if port in (None, _DEFAULT_PORTS[parsed.scheme]) else f"{host}:{port}"
