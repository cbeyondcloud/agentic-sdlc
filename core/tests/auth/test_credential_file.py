"""The credential file fallback (DF-955): used only when no OS keyring backend exists (or
when chosen), user-only readable, and never echoing what it holds."""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath

import pytest
from keyring.errors import KeyringLocked, NoKeyringError

from agentic.auth import TOKENLEAN_KEY, Credential, CredentialError, Credentials, filestore
from agentic.auth.filestore import (
    FILE_NAME,
    CredentialFileError,
    FileStore,
    _posix_problem,
    drvfs_mount,
)

SECRET = "tl-" + "file" + "-0123456789"  # gitleaks:allow - built from pieces
SERVICE = "agentic-sdlc"
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def _symlinks_work() -> bool:
    """Whether this user may make symbolic links (on Windows: Developer Mode or admin)."""
    with tempfile.TemporaryDirectory() as folder:
        try:
            os.symlink(os.path.join(folder, "target"), os.path.join(folder, "link"))
        except (OSError, NotImplementedError):
            return False
    return True


#: Symbolic-link tests run wherever links can be made, Windows included (DF-965).
SYMLINKS = pytest.mark.skipif(
    not _symlinks_work(), reason="symbolic links need Developer Mode (or admin rights) on Windows"
)


class NoKeyring:
    """What `keyring` is on a machine with no backend: every call raises NoKeyringError."""

    def get_password(self, service, username):
        raise NoKeyringError("No recommended backend was available.")

    def set_password(self, service, username, password):
        raise NoKeyringError("No recommended backend was available.")


class Ring:
    def __init__(self, stored=None, error=None):
        self.stored, self.error = dict(stored or {}), error

    def get_password(self, service, username):
        if self.error:
            raise self.error
        return self.stored.get(username)

    def set_password(self, service, username, password):
        if self.error:
            raise self.error
        self.stored[username] = password


def store(tmp_path) -> FileStore:
    return FileStore(tmp_path / ".agentic" / FILE_NAME)


# ── the file ─────────────────────────────────────────────────────────────────


def test_a_value_round_trips_and_the_file_is_versioned(tmp_path):
    files = store(tmp_path)
    files.set_password(SERVICE, TOKENLEAN_KEY, SECRET)
    assert files.get_password(SERVICE, TOKENLEAN_KEY) == SECRET
    assert files.get_password(SERVICE, "other") is None
    assert json.loads(files.path.read_text(encoding="utf-8"))["version"] == 1
    assert SECRET not in repr(files)
    assert [p.name for p in files.path.parent.iterdir()] == [FILE_NAME]  # no temporary left


def test_a_missing_file_holds_nothing(tmp_path):
    assert store(tmp_path).get_password(SERVICE, TOKENLEAN_KEY) is None


@POSIX
def test_the_file_is_user_only_and_its_new_folder_too(tmp_path):
    files = store(tmp_path)
    files.set_password(SERVICE, TOKENLEAN_KEY, SECRET)
    assert files.path.stat().st_mode & 0o777 == 0o600
    assert files.path.parent.stat().st_mode & 0o777 == 0o700


@POSIX
def test_a_file_others_can_read_is_refused_with_the_fix(tmp_path):
    files = store(tmp_path)
    files.set_password(SERVICE, TOKENLEAN_KEY, SECRET)
    files.path.chmod(0o644)
    with pytest.raises(CredentialFileError, match="chmod 600") as refused:
        files.get_password(SERVICE, TOKENLEAN_KEY)
    assert SECRET not in str(refused.value)


@SYMLINKS
def test_a_symbolic_link_is_refused(tmp_path):
    files = store(tmp_path)
    real = tmp_path / "elsewhere.json"
    real.write_text("{}", encoding="utf-8")
    files.path.parent.mkdir(parents=True)
    files.path.symlink_to(real)
    with pytest.raises(CredentialFileError, match="symbolic link"):
        files.get_password(SERVICE, TOKENLEAN_KEY)


def test_an_unreadable_file_is_refused_without_echoing_it(tmp_path):
    files = store(tmp_path)
    files.path.parent.mkdir(parents=True)
    files.path.write_text(f"not json {SECRET}", encoding="utf-8")
    if sys.platform != "win32":
        files.path.chmod(0o600)
    with pytest.raises(CredentialFileError) as refused:
        files.get_password(SERVICE, TOKENLEAN_KEY)
    assert SECRET not in str(refused.value)


def test_a_file_that_cannot_be_checked_is_a_problem_not_a_traceback(tmp_path, monkeypatch):
    files = store(tmp_path)  # DF-962: doctor shows it as a FAIL with a fix

    def denied(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_symlink", denied)
    problem = files.problem()
    assert problem and "cannot be checked" in problem and "Permission denied" in problem
    assert problem.rpartition("; ")[2]  # the fix doctor shows


def test_a_rewrite_keeps_top_level_keys_it_does_not_understand(tmp_path):  # DF-962
    files = store(tmp_path)
    files.set_password(SERVICE, TOKENLEAN_KEY, SECRET)
    data = json.loads(files.path.read_text(encoding="utf-8"))
    data["future"] = ["a", 1]
    data["note"] = "kept"
    files.path.write_text(json.dumps(data), encoding="utf-8")
    if sys.platform != "win32":
        files.path.chmod(0o600)
    files.set_password(SERVICE, "jira_token", "other")
    data = json.loads(files.path.read_text(encoding="utf-8"))
    assert data["future"] == ["a", 1] and data["note"] == "kept"
    assert data[SERVICE] == {TOKENLEAN_KEY: SECRET, "jira_token": "other"}


def stat_of(mode: int, uid: int) -> os.stat_result:
    return os.stat_result((stat.S_IFREG | mode, 0, 0, 1, uid, uid, 10, 0, 0, 0))


def test_a_file_owned_by_another_user_is_refused(tmp_path):  # DF-962, like ssh
    path = PurePosixPath("/home/ann/.agentic/credentials.json")
    problem = _posix_problem(path, stat_of(0o600, 1001), 1000, tmp_path / "none")
    assert problem and "another user" in problem and "uid 1001" in problem
    assert _posix_problem(path, stat_of(0o600, 1000), 1000, tmp_path / "none") is None


MOUNTS = (  # as WSL2 lists them: older builds show drvfs as 9p with aname=drvfs
    "rootfs / lxfs rw,noatime 0 0\n"
    "/dev/sdc /home ext4 rw,relatime 0 0\n"
    r"C:\134 /mnt/c 9p rw,noatime,dirsync,aname=drvfs;path=C:\;uid=1000 0 0"
    "\n"
    r"D:\134 /mnt/d drvfs rw,noatime,uid=1000 0 0"
    "\n"
    r"/dev/sdd /mnt/c/linux\040side ext4 rw 0 0"
    "\n"
)


@pytest.mark.parametrize(
    ("path", "mount"),
    [
        ("/mnt/c/Users/ann/.agentic/credentials.json", "/mnt/c"),
        ("/mnt/d/agentic/credentials.json", "/mnt/d"),
        ("/mnt/c/linux\040side/.agentic/credentials.json", None),  # a nested Linux mount
        ("/home/ann/.agentic/credentials.json", None),
        ("/mnt/cc/x", None),
    ],
)
def test_a_windows_drive_mount_is_recognised(tmp_path, path, mount):  # DF-960
    mounts = tmp_path / "mounts"
    mounts.write_text(MOUNTS, encoding="utf-8")
    assert drvfs_mount(PurePosixPath(path), mounts) == mount


def test_no_mount_table_means_no_windows_drive(tmp_path):
    assert drvfs_mount(PurePosixPath("/mnt/c/x"), tmp_path / "missing") is None


@pytest.mark.parametrize(
    "line",
    [
        r"C:\134 /mnt/c 9p rw,noatime,aname=drvfs;path=C:\;metadata;uid=1000 0 0",
        r"C:\134 /mnt/c drvfs rw,noatime,metadata,uid=1000 0 0",
    ],
)
def test_a_windows_drive_mounted_with_metadata_keeps_the_chmod_advice(tmp_path, line):
    mounts = tmp_path / "mounts"  # review I2: with `metadata`, chmod works there
    mounts.write_text(line + "\n", encoding="utf-8")
    path = PurePosixPath("/mnt/c/Users/ann/.agentic/credentials.json")
    assert drvfs_mount(path, mounts) is None
    problem = _posix_problem(path, stat_of(0o777, 1000), 1000, mounts)
    assert "chmod 600" in problem and "AGENTIC_HOME" not in problem


@POSIX
@pytest.mark.parametrize("how", ["relative", "symlink"])
def test_the_folder_is_resolved_before_mounts_are_matched(tmp_path, monkeypatch, how):
    drive = tmp_path / "drive"  # review M4: a home reached through a link, or relative
    (drive / "home").mkdir(parents=True)
    mounts = tmp_path / "mounts"
    mounts.write_text(f"C: {drive.resolve()} 9p rw,aname=drvfs;path=C:\\ 0 0\n", encoding="utf-8")
    if how == "symlink":
        (tmp_path / "link").symlink_to(drive / "home")
        path = tmp_path / "link" / FILE_NAME
    else:
        monkeypatch.chdir(drive)
        path = Path("home") / FILE_NAME
    assert drvfs_mount(path, mounts) == str(drive.resolve())


def test_a_file_on_a_windows_drive_names_agentic_home_not_chmod(tmp_path):  # DF-960
    mounts = tmp_path / "mounts"
    mounts.write_text(MOUNTS, encoding="utf-8")
    path = PurePosixPath("/mnt/c/Users/ann/.agentic/credentials.json")
    problem = _posix_problem(path, stat_of(0o777, 1000), 1000, mounts)
    assert problem and "AGENTIC_HOME" in problem and "chmod" not in problem
    assert "AGENTIC_HOME" in problem.rpartition("; ")[2]
    elsewhere = PurePosixPath("/home/ann/.agentic/credentials.json")
    assert "chmod 600" in _posix_problem(elsewhere, stat_of(0o644, 1000), 1000, mounts)


@POSIX
def test_a_folder_where_modes_cannot_be_set_is_refused_before_a_key_is_written(
    tmp_path, monkeypatch
):  # DF-960: what a home on /mnt/c does - the file reads 0777 right after it is written
    files = store(tmp_path)
    monkeypatch.setattr(filestore, "_mode", lambda fd: 0o777)
    with pytest.raises(CredentialFileError, match="chmod|AGENTIC_HOME"):
        files.check_writable()
    with pytest.raises(CredentialFileError):
        files.set_password(SERVICE, TOKENLEAN_KEY, SECRET)
    assert not files.exists()
    assert [p.name for p in files.path.parent.iterdir()] == []  # no temporary left


# ── the provider ─────────────────────────────────────────────────────────────


def test_without_a_file_store_nothing_changes(tmp_path):
    with pytest.raises(CredentialError, match="keyring"):
        Credentials(environ={}, keyring=NoKeyring()).find(TOKENLEAN_KEY)


def test_no_keyring_backend_stores_in_the_file_and_finds_it_there(tmp_path):
    files = store(tmp_path)
    credentials = Credentials(environ={}, keyring=NoKeyring(), file_store=files)
    assert credentials.find(TOKENLEAN_KEY) is None
    assert credentials.store(TOKENLEAN_KEY, SECRET) == "file"
    assert credentials.get(TOKENLEAN_KEY) == Credential(SECRET, "file")


def test_the_keyring_comes_first_when_it_works(tmp_path):
    files = store(tmp_path)
    ring = Ring()
    credentials = Credentials(environ={}, keyring=ring, file_store=files)
    assert credentials.store(TOKENLEAN_KEY, SECRET) == "keyring"
    assert ring.stored == {TOKENLEAN_KEY: SECRET} and not files.exists()
    assert credentials.get(TOKENLEAN_KEY).source == "keyring"


def test_a_key_stored_in_the_file_is_still_found_once_a_keyring_appears(tmp_path):
    files = store(tmp_path)
    files.set_password(SERVICE, TOKENLEAN_KEY, SECRET)
    found = Credentials(environ={}, keyring=Ring(), file_store=files).get(TOKENLEAN_KEY)
    assert found == Credential(SECRET, "file")


def test_a_keyring_that_exists_but_fails_is_an_error_not_a_silent_fallback(tmp_path):
    files = store(tmp_path)
    credentials = Credentials(
        environ={}, keyring=Ring(error=KeyringLocked("locked")), file_store=files
    )
    with pytest.raises(CredentialError, match="KeyringLocked"):
        credentials.find(TOKENLEAN_KEY)
    with pytest.raises(CredentialError):
        credentials.store(TOKENLEAN_KEY, SECRET)
    assert not files.exists()


def test_prefer_file_skips_the_keyring(tmp_path):
    files = store(tmp_path)
    ring = Ring({TOKENLEAN_KEY: "in-the-keyring"})
    credentials = Credentials(environ={}, keyring=ring, file_store=files, prefer_file=True)
    assert credentials.find(TOKENLEAN_KEY) is None
    assert credentials.store(TOKENLEAN_KEY, SECRET) == "file"
    assert ring.stored == {TOKENLEAN_KEY: "in-the-keyring"}


def test_the_environment_still_wins_and_ci_never_reads_the_file(tmp_path):
    files = store(tmp_path)
    files.set_password(SERVICE, TOKENLEAN_KEY, SECRET)
    env = {"AGENTIC_TOKENLEAN_API_KEY": "from-env"}
    assert Credentials(environ=env, keyring=NoKeyring(), file_store=files).get(
        TOKENLEAN_KEY
    ) == Credential("from-env", "env")
    ci = Credentials(environ={"CI": "true"}, keyring=NoKeyring(), file_store=files)
    assert ci.find(TOKENLEAN_KEY) is None


@POSIX
def test_a_file_too_open_is_a_credential_error_naming_the_fix(tmp_path):
    files = store(tmp_path)
    files.set_password(SERVICE, TOKENLEAN_KEY, SECRET)
    files.path.chmod(0o640)
    credentials = Credentials(environ={}, keyring=NoKeyring(), file_store=files)
    with pytest.raises(CredentialError, match="chmod 600") as refused:
        credentials.find(TOKENLEAN_KEY)
    assert SECRET not in str(refused.value)


# ── can a key be stored at all? (review F3: before an invite link is used up) ─


def test_check_store_names_the_keyring_when_it_works(tmp_path):
    credentials = Credentials(environ={}, keyring=Ring(), file_store=store(tmp_path))
    assert credentials.check_store() == "keyring"


def test_check_store_names_the_file_without_a_keyring_and_leaves_nothing(tmp_path):
    files = store(tmp_path)
    credentials = Credentials(environ={}, keyring=NoKeyring(), file_store=files)
    assert credentials.check_store() == "file"
    assert not files.exists() and list(files.path.parent.iterdir()) == []


def test_check_store_fails_when_the_file_cannot_be_written(tmp_path):
    blocker = tmp_path / ".agentic"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    credentials = Credentials(environ={}, keyring=NoKeyring(), file_store=store(tmp_path))
    with pytest.raises(CredentialError, match="credential file"):
        credentials.check_store()


def test_check_store_fails_without_a_keyring_or_a_file(tmp_path):
    with pytest.raises(CredentialError, match="keyring"):
        Credentials(environ={}, keyring=NoKeyring()).check_store()
