"""`agentic rbac` (M2-25): roles, assignments, permission checks, CSV import, service
accounts, invites and offboarding over the control plane's routes. A refusal for the
caller's role exits 2 with the server's message, a usage error 64, an invalid CSV 3."""

from __future__ import annotations

import json

import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic_cli import control_plane
from agentic_cli.config import agentic_home
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main


def _config(org: bool = True) -> None:
    home = agentic_home()
    home.mkdir(parents=True, exist_ok=True)
    text = f"org: acme\ncontrol_plane_url: {CP_URL}\n" if org else ""
    (home / "config.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
def plane(home, monkeypatch) -> FakeControlPlane:
    """A signed-in org admin of tenant acme."""
    fake = FakeControlPlane(allowed={"rbac.assign", "run.trigger"})
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    _config()
    return fake


def agentic(capsys, *args: str) -> tuple[int, dict | list | None, str]:
    code = main(["--json", *args])
    captured = capsys.readouterr()
    try:
        data = json.loads(captured.out) if captured.out.strip() else None
    except ValueError:
        data = None
    return code, data, captured.err


def _sent(plane, method, path):
    return [
        json.loads(r.content) if r.content else None
        for r in plane.requests
        if r.method == method and r.url.path == path
    ]


def test_roles_lists_the_tenants_roles(plane, capsys):
    code, data, _ = agentic(capsys, "rbac", "roles")
    assert code == 0 and [r["name"] for r in data["roles"]] == ["developer", "team-lead"]
    code, data, _ = agentic(capsys, "rbac", "roles", "developer")
    assert code == 0 and data["name"] == "developer"


def test_assign_gives_a_role_and_says_an_invite_went_out(plane, capsys):
    code, data, _ = agentic(
        capsys, "rbac", "assign", "dev@acme.example", "developer", "--team", "payments"
    )
    assert code == 0 and data["invited"] is True
    assert _sent(plane, "POST", "/v1/rbac/assignments") == [
        {"email": "dev@acme.example", "role": "developer", "team": "payments"}
    ]


def test_assignments_lists_them(plane, capsys):
    agentic(capsys, "rbac", "assign", "dev@acme.example", "developer", "--team", "payments")
    code, data, _ = agentic(capsys, "rbac", "assignments", "--team", "payments")
    assert code == 0 and [a["email"] for a in data["assignments"]] == ["dev@acme.example"]


def test_revoke_finds_the_assignment_and_deletes_it(plane, capsys):
    agentic(capsys, "rbac", "assign", "dev@acme.example", "developer", "--team", "payments")
    code, data, _ = agentic(
        capsys, "rbac", "revoke", "dev@acme.example", "developer", "--team", "payments"
    )
    assert code == 0 and data["revoked"] == plane.requests[-1].url.path.rsplit("/", 1)[1]
    assert plane.role_assignments == []
    code, _, err = agentic(
        capsys, "rbac", "revoke", "dev@acme.example", "developer", "--team", "payments"
    )
    assert code == ExitCode.ERROR and "no developer assignment" in json.loads(err)["error"]


def test_check_exits_0_when_allowed_and_2_when_not(plane, capsys):
    code, data, _ = agentic(capsys, "rbac", "check", "run.trigger", "--team", "payments")
    assert code == 0 and data["allowed"] is True
    code, data, _ = agentic(capsys, "rbac", "check", "gate.approve", "--team", "payments")
    assert code == ExitCode.REFUSED and data["allowed"] is False
    asked = _sent(plane, "POST", "/v1/rbac/check")
    assert asked[-1] == {"permission": "gate.approve", "team": "payments"}


def test_check_asks_about_another_person(plane, capsys):
    agentic(capsys, "rbac", "check", "run.trigger", "--user", "dev@acme.example")
    assert _sent(plane, "POST", "/v1/rbac/check")[-1]["user"] == "dev@acme.example"


@pytest.mark.parametrize(
    "args",
    [
        ("rbac", "assign", "not-an-email", "developer"),
        ("rbac", "assign", "dev@acme.example", "Developer!"),
        ("rbac", "check", "no.such.permission"),
        ("rbac", "assign", "dev@acme.example"),  # a missing argument
        ("rbac", "service-account", "nightly", "--team", "payments"),  # --days is required
        ("rbac", "offboard", "dev@acme.example"),  # --reason is required
    ],
)
def test_usage_errors_exit_64(plane, capsys, args):
    assert main(list(args)) == ExitCode.USAGE
    assert plane.requests == []


@pytest.mark.parametrize(
    ("allowed", "leads", "args"),
    [
        (set(), set(), ("rbac", "assign", "x@acme.example", "developer", "--team", "payments")),
        (
            set(),
            {"payments"},
            ("rbac", "assign", "x@acme.example", "developer", "--team", "search"),
        ),
        (set(), {"payments"}, ("rbac", "assign", "x@acme.example", "auditor")),
        (set(), set(), ("rbac", "service-account", "ci", "--team", "payments", "--days", "30")),
        (set(), {"payments"}, ("rbac", "offboard", "x@acme.example", "--reason", "left")),
        (set(), set(), ("rbac", "invite", "x@acme.example")),
    ],
    ids=[
        "developer-assign",
        "lead-other-team",
        "lead-org-role",
        "developer-service-account",
        "lead-offboard",
        "developer-invite",
    ],
)
def test_refusal_per_role_exits_2_with_the_servers_message(plane, capsys, allowed, leads, args):
    plane.allowed, plane.lead_teams = allowed, leads
    code, _, err = agentic(capsys, *args)
    assert code == ExitCode.REFUSED
    assert "you do not hold" in json.loads(err)["error"]


def test_a_team_lead_assigns_in_their_own_team(plane, capsys):
    plane.allowed, plane.lead_teams = set(), {"payments"}
    code, _, _ = agentic(
        capsys, "rbac", "assign", "x@acme.example", "developer", "--team", "payments"
    )
    assert code == 0


ROWS = [
    {
        "line": 2,
        "email": "ann@acme.example",
        "role": "developer",
        "team": "payments",
        "handle": None,
        "action": "add",
        "error": None,
    },
    {
        "line": 3,
        "email": "bob@acme.example",
        "role": "developer",
        "team": "payments",
        "handle": None,
        "action": "unchanged",
        "error": None,
    },
]  # two rows the fake import answers with


def test_import_is_a_dry_run_by_default_and_reads_bom_and_crlf(plane, capsys, tmp_path):
    plane.import_rows = ROWS
    csv = tmp_path / "roles.csv"
    csv.write_bytes(
        b"\xef\xbb\xbfemail,role,team\r\nann@acme.example,developer,payments\r\n"
        b"bob@acme.example,developer,payments\r\n"
    )
    code, data, _ = agentic(capsys, "rbac", "import", str(csv))
    assert code == 0 and data["applied"] is False and data["added"] == 1
    [sent] = plane.imports
    assert sent["dry_run"] is True
    assert sent["csv"].startswith("email,role,team\r\n")  # BOM dropped, bytes kept as sent
    code, data, _ = agentic(capsys, "rbac", "import", str(csv), "--apply")
    assert code == 0 and plane.imports[-1]["dry_run"] is False


def test_import_with_bad_rows_exits_3_naming_the_lines(plane, capsys, tmp_path):
    plane.import_rows = [
        {**ROWS[0], "line": 4, "action": "error", "error": "no role 'x' in this tenant"}
    ]
    csv = tmp_path / "roles.csv"
    csv.write_text("email,role\n", encoding="utf-8")
    code, data, _ = agentic(capsys, "rbac", "import", str(csv))
    assert code == ExitCode.INVALID and data["errors"] == 1
    code, _, err = agentic(capsys, "rbac", "import", str(csv), "--apply")
    assert code == ExitCode.INVALID and "line 4" in json.loads(err)["error"]


def test_import_refuses_a_file_that_is_not_utf8(plane, capsys, tmp_path):
    csv = tmp_path / "roles.csv"
    csv.write_bytes("email,role\ncafé@acme.example,developer\n".encode("cp1252"))
    code, _, err = agentic(capsys, "rbac", "import", str(csv))
    assert code == ExitCode.INVALID and "UTF-8" in json.loads(err)["error"]
    assert plane.imports == []


def test_import_of_a_missing_file_is_a_usage_error(plane, capsys, tmp_path):
    assert main(["rbac", "import", str(tmp_path / "nope.csv")]) in (
        ExitCode.USAGE,
        ExitCode.INVALID,
    )


def test_service_account_shows_its_token_once(plane, capsys):
    code, data, _ = agentic(
        capsys, "rbac", "service-account", "nightly", "--team", "payments", "--days", "30"
    )
    assert code == 0 and data["token"].startswith("acp_")
    assert _sent(plane, "POST", "/v1/service-accounts") == [
        {"name": "nightly", "team": "payments", "days": 30}
    ]


def test_invite_and_offboard(plane, capsys):
    code, data, _ = agentic(capsys, "rbac", "invite", "dev@acme.example")
    assert code == 0 and data["mailed"] is True
    code, data, _ = agentic(
        capsys, "rbac", "offboard", "dev@acme.example", "--reason", "left the company"
    )
    assert code == 0 and data["proxy_keys_revoked"] == ["0123456789abcdef"]
    assert plane.offboarded == [("dev@acme.example", "left the company")]


def test_outside_org_mode_rbac_is_refused(plane, capsys):
    _config(org=False)
    code, _, _ = agentic(capsys, "rbac", "roles")
    assert code == ExitCode.REFUSED and plane.requests == []
