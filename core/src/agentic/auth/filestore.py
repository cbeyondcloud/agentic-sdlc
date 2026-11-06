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
  * is refused on POSIX when anyone but its owner can read or write it, when another user
    owns it, or when it is a symbolic link - like ssh with a private key - with the
    command that fixes it. The read opens it without following a link (O_NOFOLLOW) and
    judges its type, owner and mode on that same descriptor (fstat), so a file swapped
    in after a check by name is never read (DF-983). On a Windows drive in WSL2 (/mnt/c,
    a drvfs mount) file modes cannot be set, so there it is refused before a key is
    written, naming AGENTIC_HOME;
  * holds `{"version": 1, "<service>": {"<name>": "<value>"}}` (a rewrite keeps any other
    top-level key, for a later version's fields); its contents never appear in an error,
    a repr or a log.

On Windows the folder's default access list already limits it to the user; Windows has
the Credential Manager, so the file is used there only when someone chooses it.
"""

import contextlib
import errno
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path, PurePath, PurePosixPath

FILE_NAME = "credentials.json"
_VERSION = 1
#: Open flags for the read: never follow a link, and never wait on a FIFO (POSIX only).
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
#: Where Linux lists its mounts; WSL2 shows a Windows drive there as drvfs (or 9p).
MOUNTS = Path("/proc/mounts")


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
        """Why the file must not be used now (too open, another user's, a link, or it
        cannot be checked), or None; never raises. Always "<what>; <fix>"."""
        try:
            if self.path.is_symlink():
                return _link(self.path)
            if not self.path.is_file() or sys.platform == "win32":
                return None
            info = self.path.stat()
        except OSError as error:
            return (
                f"{self.path} cannot be checked ({error.strerror or type(error).__name__}); "
                f"make sure you can read {self.path.parent} and the file in it"
            )
        if sys.platform == "win32":  # pragma: no cover - returned above (for type checkers)
            return None
        return _posix_problem(self.path, info, os.getuid())

    def holds_keys(self) -> bool:
        """Whether the file can be read and holds at least one key (never which)."""
        try:
            return any(isinstance(keys, dict) and keys for keys in self._read().values())
        except CredentialFileError:
            return False

    def check_writable(self) -> None:
        """Raise CredentialFileError unless a key could be stored now: the file, if there,
        is usable, and its folder takes a new file (one is made and removed)."""
        self._read()
        try:
            folder = self.path.parent
            if not folder.is_dir():
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle, probe = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=folder)
            try:
                if sys.platform != "win32":
                    os.fchmod(handle, 0o600)  # as _write does, before judging it (DF-984)
                mode = _mode(handle)
            finally:
                os.close(handle)
                os.unlink(probe)
        except OSError as error:
            raise CredentialFileError(
                f"{self.path.parent} cannot hold the credential file ({error.strerror or error})"
            ) from None
        if mode & 0o077:  # a new 0600 file reads wider: modes cannot be set here (DF-960)
            raise CredentialFileError(_too_open(self.path, mode))

    def get_password(self, service: str, username: str) -> str | None:
        value = self._read().get(service, {}).get(username)
        return value if isinstance(value, str) else None

    def set_password(self, service: str, username: str, password: str) -> None:
        data = self._load()
        keys = data.get(service)
        data[service] = {**(keys if isinstance(keys, dict) else {}), username: password}
        self._write(data)

    def _read(self) -> dict:
        """The services and their keys."""
        return {k: v for k, v in self._load().items() if isinstance(v, dict)}

    def _load(self) -> dict:
        """Every top-level key but the version, so a rewrite keeps what it does not know."""
        if not _NOFOLLOW:  # Windows: the check by name, then the read
            if not self.exists():
                return {}
            problem = self.problem()
            if problem:
                raise CredentialFileError(problem)
        try:
            raw = self._read_checked()
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise CredentialFileError(_unreadable(self.path, error)) from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as error:  # never the text: it holds the secrets
            raise CredentialFileError(_unreadable(self.path, error)) from None
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            raise CredentialFileError(
                f"{self.path} is not an agentic credential file; move it away and run "
                "`agentic login` again"
            )
        return {k: v for k, v in data.items() if k != "version"}

    def _read_checked(self) -> bytes:
        """The file's bytes. On POSIX it is opened without following a link and judged by
        fstat on the descriptor read from (DF-983): a link, FIFO or other user's file
        swapped in after a check by name is refused, not read."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | _NOFOLLOW | _NONBLOCK
        try:
            handle = os.open(self.path, flags)
        except OSError as error:
            if _NOFOLLOW and error.errno == errno.ELOOP:  # the last part is a link
                raise CredentialFileError(_link(self.path)) from None
            raise
        with os.fdopen(handle, "rb") as file:
            if sys.platform != "win32":
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise CredentialFileError(
                        f"{self.path} is not a regular file; move it away and run "
                        "`agentic login` again"
                    )
                problem = _posix_problem(self.path, info, os.getuid())
                if problem:
                    raise CredentialFileError(problem)
            return file.read()

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
                mode = _mode(out.fileno())
                if mode & 0o077:  # modes cannot be set here (DF-960): never write a key
                    raise CredentialFileError(_too_open(self.path, mode))
                out.write(text.encode("utf-8"))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


def _link(path: PurePath) -> str:
    return f"{path} is a symbolic link; replace it with a regular file"


def _unreadable(path: PurePath, error: Exception) -> str:
    return (
        f"{path} cannot be read ({type(error).__name__}); move it away and run "
        "`agentic login` again"
    )


def _mode(handle: int) -> int:
    """The permission bits an open file reads with (0 on Windows, which has no modes)."""
    return 0 if sys.platform == "win32" else stat.S_IMODE(os.fstat(handle).st_mode)


def _posix_problem(
    path: PurePath, info: os.stat_result, uid: int, mounts: Path | None = None
) -> str | None:
    """Why a POSIX credential file with this `stat` must not be used by `uid`, or None."""
    if info.st_uid == 0 != uid:  # after `sudo agentic login`: moving it needs sudo (DF-982)
        return (
            f"{path} belongs to root, not to you (uid {uid}); run `sudo chown $USER {path}` "
            f"(or `sudo rm {path}` and run `agentic login` again)"
        )
    if info.st_uid != uid:
        return (
            f"{path} belongs to another user (uid {info.st_uid}, not yours, {uid}); move it "
            "away and run `agentic login` again"
        )
    mode = stat.S_IMODE(info.st_mode)
    return _too_open(path, mode, mounts) if mode & 0o077 else None


def _too_open(path: PurePath, mode: int, mounts: Path | None = None) -> str:
    drive = drvfs_mount(path, mounts)
    if drive is not None:  # chmod cannot help there: the mount reports 0777 for every file
        return (
            f"{path} is on the Windows drive mounted at {drive}, where file modes cannot be "
            f"set (it reads {mode:04o}); set AGENTIC_HOME to a folder in the Linux file "
            "system, for example `export AGENTIC_HOME=/home/$USER/.agentic`"
        )
    return f"{path} can be read or changed by other users (mode {mode:04o}); run `chmod 600 {path}`"


def drvfs_mount(path: PurePath, mounts: Path | None = None) -> str | None:
    """The mount point when `path` is on a Windows drive under WSL (drvfs, listed as `drvfs`
    or as `9p` with `aname=drvfs`) where file modes cannot be set, else None - also where
    there is no mount table, or the drive is mounted with `metadata` (chmod works there).
    The folder is resolved first, so a home reached through a link or a relative
    AGENTIC_HOME is matched where it really is (review M4)."""
    try:
        table = (MOUNTS if mounts is None else mounts).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    where = os.fspath(path)
    if sys.platform != "win32":  # on Windows only the tests call this, with POSIX paths
        where = os.path.realpath(where)
    target = PurePosixPath(where)
    best: tuple[PurePosixPath, bool] | None = None
    for line in table.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        point = PurePosixPath(_OCTAL.sub(lambda m: chr(int(m[1], 8)), fields[1]))
        if point != target and point not in target.parents:
            continue
        if best is None or len(point.parts) >= len(best[0].parts):  # the innermost mount
            kind, options = fields[2], set(re.split(r"[,;]", fields[3]))  # 9p: ';' inside
            drvfs = kind == "drvfs" or (kind == "9p" and "aname=drvfs" in options)
            best = (point, drvfs and "metadata" not in options)  # review I2
    return str(best[0]) if best is not None and best[1] else None


#: /proc/mounts writes a space in a mount point as \040 (octal).
_OCTAL = re.compile(r"\\([0-7]{3})")
