"""Where the CLI keeps keys (DF-955): the OS keyring first; on a machine with no keyring
backend (WSL2 without a Secret Service) the user-only credential file, with a warning,
unless the person (`credential_store`, `AGENTIC_CREDENTIAL_STORE`) or the cached org
policy (`credential_file_fallback: false`) forbids it; and `agentic doctor` shows which."""

import json
import sys

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import NoKeyringError

from agentic.auth import FILE_NAME, SERVICE
from agentic_cli.config import agentic_home
from agentic_cli.credential_store import STORE_ENV, cli_credentials, store_choice
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

KEY = "tl-" + "store" + "-0123456789"  # gitleaks:allow - built from pieces


class NoBackend(KeyringBackend):
    """`keyring` on a machine with no usable backend."""

    priority = 1

    def get_password(self, service, username):
        raise NoKeyringError("No recommended backend was available.")

    def set_password(self, service, username, password):
        raise NoKeyringError("No recommended backend was available.")

    def delete_password(self, service, username):
        raise NoKeyringError("No recommended backend was available.")


@pytest.fixture
def no_keyring(home, monkeypatch):
    previous = keyring.get_keyring()
    keyring.set_keyring(NoBackend())
    monkeypatch.delenv(STORE_ENV, raising=False)
    yield
    keyring.set_keyring(previous)


def write_config(text: str) -> None:
    folder = agentic_home()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(text, encoding="utf-8")


def cache_policy(org: str, text: str) -> None:
    folder = agentic_home() / "policy" / org / "repo" / "policy"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "org.yaml").write_text(text, encoding="utf-8")


def login(capsys, *args):
    code = main(["--json", "login", "--key-stdin", *args])
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out.strip() else None, out.err


# ── the choice ───────────────────────────────────────────────────────────────


def test_by_default_the_file_is_a_fallback(no_keyring):
    choice = store_choice()
    assert (choice.mode, choice.forbidden) == ("auto", None)
    assert choice.path == agentic_home() / FILE_NAME
    assert cli_credentials().file_store is not None


@pytest.mark.parametrize("where", ["env", "config"])
def test_the_person_may_choose_keyring_only_or_the_file(no_keyring, monkeypatch, where):
    for mode in ("keyring", "file"):
        if where == "env":
            monkeypatch.setenv(STORE_ENV, mode)
        else:
            write_config(f"credential_store: {mode}\n")
        credentials = cli_credentials()
        assert (credentials.file_store is None) is (mode == "keyring")
        assert credentials.prefer_file is (mode == "file")


def test_the_environment_wins_over_the_config(no_keyring, monkeypatch):
    write_config("credential_store: file\n")
    monkeypatch.setenv(STORE_ENV, "keyring")
    assert store_choice().mode == "keyring"


def test_an_unknown_choice_is_invalid(no_keyring, monkeypatch):
    monkeypatch.setenv(STORE_ENV, "plaintext")
    with pytest.raises(CliError) as raised:
        store_choice()
    assert raised.value.code == ExitCode.INVALID and "auto, keyring or file" in str(raised.value)


def test_a_cached_org_policy_can_forbid_the_file(no_keyring):
    write_config("org: acme\n")
    cache_policy("acme", "credential_file_fallback: false\n")
    choice = store_choice()
    assert "acme" in choice.forbidden and "credential_file_fallback" in choice.forbidden
    assert cli_credentials().file_store is None


def test_a_cached_org_policy_that_cannot_be_read_forbids_it_too(no_keyring):
    write_config("org: acme\n")
    cache_policy("acme", "credential_file_fallback: [unclosed\n")
    assert store_choice().forbidden


def test_an_org_policy_that_allows_it_or_says_nothing_allows_it(no_keyring):
    write_config("org: acme\n")
    cache_policy("acme", "allow_repo_pipelines: false\n")
    assert store_choice().forbidden is None


# ── login ────────────────────────────────────────────────────────────────────


def test_login_without_a_keyring_stores_in_the_file_and_warns(no_keyring, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
    code, data, err = login(capsys, "--for", "jira")
    assert code == ExitCode.OK, err
    path = agentic_home() / FILE_NAME
    assert data["where"] == "file" and data["credential_file"] == str(path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored[SERVICE]["jira_token"] == KEY
    assert KEY not in err and KEY not in json.dumps(data)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
    assert main(["login", "--key-stdin", "--for", "jira"]) == ExitCode.OK
    shown = capsys.readouterr()
    assert str(path) in shown.err and "not encrypted" in shown.err
    assert KEY not in shown.err + shown.out
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600


def test_login_without_a_keyring_is_refused_when_policy_forbids_the_file(
    no_keyring, capsys, monkeypatch
):
    write_config("org: acme\n")
    cache_policy("acme", "credential_file_fallback: false\n")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
    code, _, err = login(capsys, "--for", "jira")
    assert code == ExitCode.ERROR
    assert "credential_file_fallback" in err and KEY not in err
    assert not (agentic_home() / FILE_NAME).exists()


def test_a_key_in_the_file_is_found_by_later_commands(no_keyring, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
    login(capsys, "--for", "jira")
    assert cli_credentials().get("jira_token").source == "file"


# ── doctor ───────────────────────────────────────────────────────────────────


def doctor_checks(capsys) -> dict:
    main(["--json", "doctor"])
    return {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}


def test_doctor_names_the_file_fallback(no_keyring, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    checks = doctor_checks(capsys)
    assert checks["keyring"]["status"] == "warn"
    assert FILE_NAME in checks["keyring"]["detail"]
    assert checks["credential file"]["status"] == "ok"  # allowed, nothing stored yet
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
    login(capsys, "--for", "jira")
    checks = doctor_checks(capsys)
    assert checks["credential file"]["status"] == "warn"
    assert "not encrypted" in checks["credential file"]["detail"]
    assert KEY not in json.dumps(checks)


def test_doctor_fails_a_file_the_org_forbids(no_keyring, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
    login(capsys, "--for", "jira")
    write_config("org: acme\n")
    cache_policy("acme", "credential_file_fallback: false\n")
    checks = doctor_checks(capsys)
    assert checks["credential file"]["status"] == "fail"
    assert "delete" in checks["credential file"]["fix"]
    assert checks["keyring"]["status"] == "fail"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_doctor_fails_a_file_others_can_read(no_keyring, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
    login(capsys, "--for", "jira")
    (agentic_home() / FILE_NAME).chmod(0o644)
    checks = doctor_checks(capsys)
    assert checks["credential file"]["status"] == "fail"
    assert "chmod 600" in checks["credential file"]["fix"] + checks["credential file"]["detail"]


# ── an unknown choice never crashes the diagnostics (review F4) ──────────────


def test_doctor_reports_an_unknown_choice_as_a_failing_check(
    no_keyring, capsys, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(STORE_ENV, "bogus")
    checks = doctor_checks(capsys)
    assert checks["credential file"]["status"] == "fail"
    assert "auto, keyring or file" in checks["credential file"]["detail"]


def test_the_support_bundle_masks_file_keys_even_when_the_choice_is_unknown(
    no_keyring, capsys, monkeypatch
):
    from agentic_cli.support_bundle import _cleaner

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
    login(capsys)  # the gateway key, into the file
    monkeypatch.setenv(STORE_ENV, "bogus")
    cleaned = _cleaner(dict(__import__("os").environ)).text(f"the key is {KEY}")
    assert KEY not in cleaned


def test_a_chosen_file_is_not_called_a_missing_keyring(home, capsys, monkeypatch):  # review F7
    monkeypatch.setenv(STORE_ENV, "file")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
    assert main(["login", "--key-stdin", "--for", "jira"]) == ExitCode.OK
    err = capsys.readouterr().err
    assert "credential_store is file" in err and "no OS keyring" not in err


def test_doctor_fails_a_file_it_cannot_check_with_a_fix(no_keyring, monkeypatch):  # DF-962
    from pathlib import Path

    from agentic_cli import doctor

    real = Path.is_symlink

    def denied(self):
        if self.name == FILE_NAME:
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(Path, "is_symlink", denied)
    check = doctor._credential_file(store_choice())
    assert check.status == "fail" and "cannot be checked" in check.detail and check.fix


# ── a file left from before a ban (DF-960) ───────────────────────────────────


class Memory(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.stored = {}

    def get_password(self, service, username):
        return self.stored.get((service, username))

    def set_password(self, service, username, password):
        self.stored[(service, username)] = password

    def delete_password(self, service, username):
        self.stored.pop((service, username), None)


def leave_a_forbidden_file() -> None:
    from agentic.auth import FileStore

    FileStore(agentic_home() / FILE_NAME).set_password(SERVICE, "jira_token", KEY)
    write_config("org: acme\n")
    cache_policy("acme", "credential_file_fallback: false\n")


def test_login_warns_once_about_a_file_the_org_now_forbids(home, capsys, monkeypatch):
    monkeypatch.delenv(STORE_ENV, raising=False)
    leave_a_forbidden_file()
    previous = keyring.get_keyring()
    keyring.set_keyring(Memory())
    try:
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
        assert main(["login", "--key-stdin", "--for", "jira"]) == ExitCode.OK
    finally:
        keyring.set_keyring(previous)
    err = capsys.readouterr().err
    path = str(agentic_home() / FILE_NAME)
    assert err.count("still holds keys") == 1 and path in err
    assert "credential_file_fallback" in err and "delete it" in err and KEY not in err


def test_no_warning_without_a_file_or_when_the_file_is_allowed(home, monkeypatch):
    from agentic_cli.credential_store import forbidden_file_warning

    monkeypatch.delenv(STORE_ENV, raising=False)
    write_config("org: acme\n")
    cache_policy("acme", "credential_file_fallback: false\n")
    assert forbidden_file_warning() is None  # nothing on disk
    leave_a_forbidden_file()
    assert "still holds keys" in forbidden_file_warning()
    cache_policy("acme", "credential_file_fallback: true\n")
    assert forbidden_file_warning() is None


# ── more ways into the file, through the CLI (DF-965) ────────────────────────


def stored_in_file(name: str) -> str | None:
    data = json.loads((agentic_home() / FILE_NAME).read_text(encoding="utf-8"))
    return data[SERVICE].get(name)


@pytest.fixture
def plane(monkeypatch):
    from cp_fakes import CP_URL, FakeControlPlane

    from agentic_cli import control_plane

    fake = FakeControlPlane()
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_URL", CP_URL)
    return fake


def test_login_for_the_control_plane_stores_into_the_file(no_keyring, plane, capsys, monkeypatch):
    from cp_fakes import TOKEN

    from agentic_cli.control_plane import CONTROL_PLANE_TOKEN

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(TOKEN + "\n"))
    code, data, err = login(capsys, "--for", "control-plane")
    assert code == ExitCode.OK, err
    assert data["where"] == "file" and data["credential_file"] == str(agentic_home() / FILE_NAME)
    assert stored_in_file(CONTROL_PLANE_TOKEN) == TOKEN and TOKEN not in json.dumps(data)


def test_login_rotate_stores_the_new_key_into_the_file(no_keyring, plane, capsys, monkeypatch):
    from cp_fakes import CP_URL, TOKEN

    from agentic.auth import TOKENLEAN_KEY

    write_config(f"control_plane_url: {CP_URL}\n")
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    plane.proxy_key = KEY
    code = main(["--json", "login", "--rotate"])
    out = capsys.readouterr()
    assert code == ExitCode.OK, out.err
    data = json.loads(out.out)
    assert data["where"] == "file" and data["rotated"] is True
    assert stored_in_file(TOKENLEAN_KEY) == KEY and KEY not in out.out + out.err


def test_a_chosen_file_is_used_although_a_keyring_works(home, capsys, monkeypatch):
    monkeypatch.delenv(STORE_ENV, raising=False)
    write_config("credential_store: file\n")
    ring = Memory()
    previous = keyring.get_keyring()
    keyring.set_keyring(ring)
    try:
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(KEY + "\n"))
        code, data, err = login(capsys, "--for", "jira")
        assert code == ExitCode.OK, err
        assert data["where"] == "file" and stored_in_file("jira_token") == KEY
        assert ring.stored == {}  # the keyring is skipped, not written
        assert cli_credentials().get("jira_token").source == "file"
    finally:
        keyring.set_keyring(previous)
