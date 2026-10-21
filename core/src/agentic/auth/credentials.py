"""Finding a credential: the environment first, then the OS keyring (M1-48, spec §13).

v1's credential is the user's TokenLean proxy key (D12). Lookup order:

  1. the environment variable `AGENTIC_<NAME>` - how CI and service accounts
     pass it; it always wins;
  2. in CI (`CI`, or another system's marker in `CI_MARKERS`, set to anything
     but empty/0/false/no) the search stops there:
     a CI runner has no keychain, and a keychain prompt would hang the job;
  3. the OS keyring (Windows Credential Manager, macOS Keychain, Secret
     Service) through `keyring` (MIT), service `agentic-sdlc` - where
     `agentic login` stores it (`store`).

A missing key, or a keyring that cannot be used, is a `CredentialError` that
says how to fix it. A credential's value never appears in a repr or an error.
The provider is a small protocol, so OIDC/SSO (phase 3) can replace it.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

import keyring

SERVICE = "agentic-sdlc"
#: A backend can fail below keyring's own errors (D-Bus, a locked store, the OS API -
#: Windows raises pywintypes.error, which is none of KeyringError, OSError or
#: RuntimeError), so every exception from a keyring call becomes a CredentialError
#: naming only its type (DF-157): its message could carry the secret.
_KEYRING_ERRORS = (Exception,)
TOKENLEAN_KEY = "tokenlean_api_key"
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NOT_CI = frozenset({"", "0", "false", "no", "off"})
#: Set by CI systems; most set `CI`, these do not always (Azure Pipelines, Jenkins...).
CI_MARKERS = (
    "CI",
    "TF_BUILD",
    "JENKINS_URL",
    "BUILDKITE",
    "TEAMCITY_VERSION",
    "GITLAB_CI",
    "GITHUB_ACTIONS",
    "CIRCLECI",
    "BITBUCKET_BUILD_NUMBER",
    "CODEBUILD_BUILD_ID",
)


class CredentialError(Exception):
    """A credential that cannot be found or stored; the message says what to do."""


@dataclass(frozen=True)
class Credential:
    value: str = field(repr=False)
    source: Literal["env", "keyring"]


class Keyring(Protocol):
    """The `keyring` module, or one keyring backend."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...


class CredentialProvider(Protocol):
    def get(self, name: str) -> Credential: ...


def env_var(name: str) -> str:
    """The environment variable for credential `name`: `AGENTIC_<NAME>`."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f"a credential name is a lower-case slug (a-z, 0-9, _), not {name!r}")
    return f"AGENTIC_{name.upper()}"


class Credentials:
    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        keyring: Keyring | None = None,
        service: str = SERVICE,
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self._keyring, self.service = keyring, service

    @property
    def in_ci(self) -> bool:
        return self.ci_marker is not None

    @property
    def ci_marker(self) -> str | None:
        """The first CI marker variable set to a true value, or None outside CI."""
        for name in CI_MARKERS:
            if self.environ.get(name, "").strip().lower() not in _NOT_CI:
                return name
        return None

    def find(self, name: str, *, login: str = "agentic login") -> Credential | None:
        """The credential, or None if neither place has it. Raises CredentialError
        when the keyring cannot be used; `login` is the command its message names for
        storing this credential."""
        value = self.environ.get(env_var(name), "").strip()
        if value:
            return Credential(value, "env")
        if self.in_ci:
            return None
        try:
            stored = self._ring().get_password(self.service, name)
        except _KEYRING_ERRORS as error:
            raise CredentialError(
                f"the OS keyring could not be read ({type(error).__name__}). Set "
                f"{env_var(name)} instead, or fix the keyring and run `{login}`."
            ) from error
        stored = (stored or "").strip()
        return Credential(stored, "keyring") if stored else None

    def get(self, name: str, *, login: str = "agentic login") -> Credential:
        """The credential; a CredentialError saying how to provide it when absent (with
        `login`, the command that stores this credential, e.g. `agentic login --for X`)."""
        found = self.find(name, login=login)
        if found is not None:
            return found
        if self.in_ci:
            raise CredentialError(
                f"no {name} in this CI job: set the {env_var(name)} environment variable "
                f"from the CI system's secrets (the keyring is not used in CI; "
                f"{self.ci_marker} is set - unset it if this is not CI)."
            )
        raise CredentialError(
            f"no {name} found: set the {env_var(name)} environment variable, or run "
            f"`{login}` to store it in the OS keyring."
        )

    def store(self, name: str, value: str) -> None:
        """Save a credential in the OS keyring (what `agentic login` does)."""
        env_var(name)  # the name check
        if not value or not value.strip():
            raise CredentialError(f"refusing to store an empty {name}")
        try:
            self._ring().set_password(self.service, name, value.strip())
        except _KEYRING_ERRORS as error:
            raise CredentialError(
                f"the OS keyring could not store {name} ({type(error).__name__}); set "
                f"{env_var(name)} in the environment instead."
            ) from error

    def _ring(self) -> Keyring:
        # the module: it picks the OS backend on first use, so CI never initialises one
        return self._keyring if self._keyring is not None else keyring
