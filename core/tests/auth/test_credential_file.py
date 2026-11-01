"""The credential file fallback (DF-955): used only when no OS keyring backend exists (or
when chosen), user-only readable, and never echoing what it holds."""

import json
import sys

import pytest
from keyring.errors import KeyringLocked, NoKeyringError

from agentic.auth import TOKENLEAN_KEY, Credential, CredentialError, Credentials
from agentic.auth.filestore import FILE_NAME, CredentialFileError, FileStore

SECRET = "tl-" + "file" + "-0123456789"  # gitleaks:allow - built from pieces
SERVICE = "agentic-sdlc"
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


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


@POSIX
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
