"""The credential file: where keys are kept when a machine has no OS keyring (DF-955).

A WSL2 Ubuntu without a desktop session, a headless Linux box or a dev container usually
has no Secret Service, so `keyring` has no backend to store a key in. Like the GitHub CLI
and glab, agentic then falls back to a plaintext file - `~/.agentic/credentials.json` -
that only the user can read. It is not encrypted: a key that decrypts it would have to
sit beside it. The OS keyring always comes first; the caller decides whether this file
may be used at all (org policy, `AGENTIC_CREDENTIAL_STORE`), and says so loudly.

The file:

  * is created with mode 0600 inside a folder that is 0700 when agentic creates it, and
    replaced in one step (a temporary file, then a rename), so a crash never leaves half
    a file;
  * is refused on POSIX when anyone but its owner can read or write it, or when it is a
    symbolic link - like ssh with a private key - with the command that fixes it;
  * holds `{"version": 1, "<service>": {"<name>": "<value>"}}`; its contents never appear
    in an error, a repr or a log.

On Windows the folder's default access list already limits it to the user; Windows has
the Credential Manager, so the file is used there only when someone chooses it.
"""

import contextlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

FILE_NAME = "credentials.json"
_VERSION = 1


class CredentialFileError(Exception):
    """The credential file cannot be used; the message says how to fix it."""


class FileStore:
    """The keyring protocol (`get_password`, `set_password`) over one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def __repr__(self) -> str:
        return f"FileStore({str(self.path)!r})"

    def exists(self) -> bool:
        return self.path.is_file() or self.path.is_symlink()

    def problem(self) -> str | None:
        """Why the file must not be used now (too open, a link), or None."""
        if self.path.is_symlink():
            return f"{self.path} is a symbolic link; replace it with a regular file"
        if not self.path.is_file() or sys.platform == "win32":
            return None
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            return (
                f"{self.path} can be read or changed by other users (mode {mode:04o}); "
                f"run `chmod 600 {self.path}`"
            )
        return None

    def check_writable(self) -> None:
        """Raise CredentialFileError unless a key could be stored now: the file, if there,
        is usable, and its folder takes a new file (one is made and removed)."""
        self._read()
        try:
            folder = self.path.parent
            if not folder.is_dir():
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle, probe = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=folder)
            os.close(handle)
            os.unlink(probe)
        except OSError as error:
            raise CredentialFileError(
                f"{self.path.parent} cannot hold the credential file ({error.strerror or error})"
            ) from None

    def get_password(self, service: str, username: str) -> str | None:
        value = self._read().get(service, {}).get(username)
        return value if isinstance(value, str) else None

    def set_password(self, service: str, username: str, password: str) -> None:
        data = self._read()
        data.setdefault(service, {})[username] = password
        self._write(data)

    def _read(self) -> dict:
        if not self.exists():
            return {}
        problem = self.problem()
        if problem:
            raise CredentialFileError(problem)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:  # never the text: it holds the secrets
            raise CredentialFileError(
                f"{self.path} cannot be read ({type(error).__name__}); move it away and run "
                "`agentic login` again"
            ) from None
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            raise CredentialFileError(
                f"{self.path} is not an agentic credential file; move it away and run "
                "`agentic login` again"
            )
        return {k: v for k, v in data.items() if k != "version" and isinstance(v, dict)}

    def _write(self, data: dict) -> None:
        folder = self.path.parent
        if not folder.is_dir():
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = json.dumps({"version": _VERSION, **data}, indent=2, sort_keys=True) + "\n"
        handle, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=folder)
        try:
            with os.fdopen(handle, "wb") as out:
                if sys.platform != "win32":
                    os.fchmod(out.fileno(), 0o600)  # mkstemp makes it 0600; never wider
                out.write(text.encode("utf-8"))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
