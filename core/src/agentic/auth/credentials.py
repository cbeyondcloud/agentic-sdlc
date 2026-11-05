"""Finding a credential: the environment first, then the OS keyring (M1-48, spec §13).

v1's credential is the user's TokenLean proxy key (D12). Lookup order:

  1. the environment variable `AGENTIC_<NAME>` - how CI and service accounts
     pass it; it always wins;
  2. in CI (`CI`, or another system's marker in `CI_MARKERS`, set to anything
     but empty/0/false/no) the search stops there:
     a CI runner has no keychain, and a keychain prompt would hang the job;
  3. the OS keyring (Windows Credential Manager, macOS Keychain, Secret
     Service) through `keyring` (MIT), service `agentic-sdlc` - where
     `agentic login` stores it (`store`);
  4. only when the caller gives a `file_store` (DF-955): the credential file
     (`agentic.auth.filestore`). It is read after the keyring, and written only when
     `keyring` has no backend at all (`NoKeyringError`: a WSL2 or headless Linux
     without a Secret Service) - a keyring that exists but fails (locked, D-Bus
     errors) stays an error, never a silent downgrade. With `prefer_file` the
     keyring is skipped (the person chose the file).

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
from keyring.errors import NoKeyringError

from agentic.auth.filestore import CredentialFileError, FileStore

SERVICE = "agentic-sdlc"
#: A backend can fail below keyring's own errors (D-Bus, a locked store, the OS API -
#: Windows raises pywintypes.error, which is none of KeyringError, OSError or
#: RuntimeError), so every exception from a keyring call becomes a CredentialError
#: naming only its type (DF-157): its message could carry the secret.
_KEYRING_ERRORS = (Exception,)
TOKENLEAN_KEY = "tokenlean_api_key"
_NO_STORE = (
    "the OS keyring cannot be used ({}), so a key cannot be stored here; fix the keyring "
    "or set the AGENTIC_* variables instead"
)
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
    source: Literal["env", "keyring", "file"]


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
        file_store: FileStore | None = None,
        prefer_file: bool = False,
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self._keyring, self.service = keyring, service
        self.file_store = file_store
        self.prefer_file = prefer_file and file_store is not None

    def keyring_only(self) -> "Credentials":
        """The same environment, keyring and service without the credential file: what
        asks whether the OS keyring itself works (`agentic doctor`)."""
        return Credentials(environ=self.environ, keyring=self._keyring, service=self.service)

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
        stored = None
        if not self.prefer_file:
            try:
                stored = self._ring().get_password(self.service, name)
            except NoKeyringError as error:
                if self.file_store is None:
                    raise self._unreadable(name, login, error) from error
            except _KEYRING_ERRORS as error:
                raise self._unreadable(name, login, error) from error
        stored = (stored or "").strip()
        if stored:
            return Credential(stored, "keyring")
        if self.file_store is None:
            return None
        try:
            stored = (self.file_store.get_password(self.service, name) or "").strip()
        except CredentialFileError as error:
            raise CredentialError(f"{error} (it holds keys `{login}` stored)") from None
        return Credential(stored, "file") if stored else None

    def check_store(self) -> Literal["keyring", "file"]:
        """Where `store` would put a key now, checked without storing one - for a caller
        that must not spend something first (an invite link works once). A
        CredentialError when nothing could take it."""
        if not self.prefer_file:
            try:
                self._ring().get_password(self.service, "agentic_store_check")
                return "keyring"
            except NoKeyringError as error:
                if self.file_store is None:
                    raise CredentialError(_NO_STORE.format(type(error).__name__)) from error
            except _KEYRING_ERRORS as error:
                raise CredentialError(_NO_STORE.format(type(error).__name__)) from error
        assert self.file_store is not None
        try:
            self.file_store.check_writable()
        except CredentialFileError as error:
            raise CredentialError(f"the credential file cannot be used: {error}") from None
        return "file"

    def _unreadable(self, name: str, login: str, error: Exception) -> CredentialError:
        return CredentialError(
            f"the OS keyring could not be read ({type(error).__name__}). Set "
            f"{env_var(name)} instead, or fix the keyring and run `{login}`."
        )

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

    def store(self, name: str, value: str) -> Literal["keyring", "file"]:
        """Save a credential (what `agentic login` does): in the OS keyring, or in the
        credential file when there is no keyring backend (or `prefer_file`) and a file
        store was given. Returns where it went."""
        env_var(name)  # the name check
        if not value or not value.strip():
            raise CredentialError(f"refusing to store an empty {name}")
        if not self.prefer_file:
            try:
                self._ring().set_password(self.service, name, value.strip())
                return "keyring"
            except NoKeyringError as error:
                if self.file_store is None:
                    raise self._unstorable(name, error) from error
            except _KEYRING_ERRORS as error:
                raise self._unstorable(name, error) from error
        assert self.file_store is not None
        try:
            self.file_store.set_password(self.service, name, value.strip())
        except (CredentialFileError, OSError) as error:
            raise CredentialError(
                f"the credential file could not store {name} ({type(error).__name__}: "
                f"{error if isinstance(error, CredentialFileError) else error.strerror}); "
                f"set {env_var(name)} in the environment instead."
            ) from None
        return "file"

    def _unstorable(self, name: str, error: Exception) -> CredentialError:
        return CredentialError(
            f"the OS keyring could not store {name} ({type(error).__name__}); set "
            f"{env_var(name)} in the environment instead."
        )

    def _ring(self) -> Keyring:
        # the module: it picks the OS backend on first use, so CI never initialises one
        return self._keyring if self._keyring is not None else keyring
