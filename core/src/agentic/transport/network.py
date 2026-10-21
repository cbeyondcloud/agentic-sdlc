"""Corporate networks: proxies and certificate authorities (M1-14, spec §15).

Proxies: httpx2 reads `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and `NO_PROXY`
when `trust_env` is on, so nothing is re-implemented here.

Certificates: corporate networks re-sign TLS with their own CA, usually
installed in the operating system's store. `truststore` makes Python verify
against that store on Windows, macOS and Linux; an extra CA bundle (a file, or
`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` when `trust_env` is on) is added on top.

On Linux the system CAs are loaded through OpenSSL, which reads `SSL_CERT_FILE`
itself, so there that one variable is honoured even with `trust_env` off.
"""

import os
import ssl
from dataclasses import dataclass
from pathlib import Path

import truststore

#: Environment variables that name an extra CA bundle (read when trust_env is on).
CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


class NetworkError(ValueError):
    """The network settings cannot be used (e.g. a CA bundle that does not exist)."""


@dataclass(frozen=True)
class NetworkSettings:
    ca_bundle: Path | None = None  # an extra CA bundle (PEM) to trust
    system_trust: bool = True  # verify against the OS certificate store
    trust_env: bool = True  # honour proxy and CA environment variables


def ssl_context(settings: NetworkSettings) -> ssl.SSLContext:
    """The TLS context every model client verifies servers with."""
    if settings.system_trust:
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    else:
        context = ssl.create_default_context()
    if settings.ca_bundle is not None:
        _trust(context, Path(settings.ca_bundle), "CA bundle")
    if settings.trust_env:
        for name in CA_ENV_VARS:
            if path := os.environ.get(name, ""):
                _trust(context, Path(path), f"{name} names a CA file that")
    return context


def _trust(context: ssl.SSLContext, path: Path, what: str) -> None:
    if not path.is_file():
        raise NetworkError(f"{what} does not exist: {path}")
    try:
        context.load_verify_locations(cafile=str(path))
    except (ssl.SSLError, OSError, ValueError) as error:
        raise NetworkError(
            f"{what} is not a readable PEM certificate file: {path} ({error})"
        ) from error
