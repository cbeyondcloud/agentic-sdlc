"""Bulk role import from CSV (M2-14, Review Focus 3): a dry run shows the diff and writes
nothing; applying is all or nothing, additive and idempotent; every bad row is reported with
its line number, whatever the file's BOM, line endings or quoting."""

import pytest
from apiclient import assign, member
from sqlalchemy import select

from agentic_cp.audit import read_records
from agentic_cp.rbac.importer import parse
from agentic_cp.tables import users
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

GOOD = (
    "email,role,team,handle\n"
    "ann@a.example.test,developer,payments,\n"
    "bob@a.example.test,approver,payments,git.example.test:bob\n"
    "cat@a.example.test,auditor,,\n"
)


def test_parse_reads_rows_with_their_line_numbers():
    rows, errors = parse(GOOD)
    assert errors == []
    assert [(r.line, r.email, r.role, r.team, r.handle) for r in rows] == [
        (2, "ann@a.example.test", "developer", "payments", None),
        (3, "bob@a.example.test", "approver", "payments", "git.example.test:bob"),
        (4, "cat@a.example.test", "auditor", None, None),
    ]


def test_parse_handles_bom_crlf_quoted_newlines_and_blank_lines():
    text = (
        "﻿Email,Role,Team\r\n"
        "ann@a.example.test,developer,payments\r\n"
        "\r\n"
        '"bob@a.example.test","approver","pay\r\nments"\r\n'
        "cat@a.example.test,nosuch-role!,payments\r\n"
    )
    rows, errors = parse(text)
    assert [r.line for r in rows] == [2]
    assert [(e.line, e.action) for e in errors] == [(4, "error"), (6, "error")]
    assert "team" in errors[0].error and "role" in errors[1].error


@pytest.mark.parametrize(
    ("text", "line", "words"),
    [
        ("", 1, "header"),
        ("name,role\nx,y\n", 1, "email"),
        ("email,role,colour\nx@a.example.test,developer,red\n", 1, "colour"),
        ("email,role\nnot-an-email,developer\n", 2, "email"),
        ("email,role\na@a.example.test,developer,extra\n", 2, "fields"),
        (
            "email,role,team\na@a.example.test,developer,payments\nA@a.example.test,developer,payments\n",
            3,
            "line 2",
        ),
        ("email,role,handle\na@a.example.test,developer,bob\n", 2, "handle"),
    ],
)
def test_parse_reports_bad_rows_with_their_line(text, line, words):
    _, errors = parse(text)
    assert errors and errors[0].line == line and words in errors[0].error


async def _import(api, token, text, *, dry_run=True):
    return await api.post("/v1/rbac/import", token, {"csv": text, "dry_run": dry_run})


async def _chain(api, tenant=None):
    async with tenant_tx(api.db.app, (tenant or api.a).id) as conn:
        return await read_records(conn)


async def test_dry_run_shows_the_diff_and_writes_nothing(api):
    await assign(api, api.a.admin_token, "ann@a.example.test", "developer", "payments")
    before = len(await _chain(api))
    reply = await _import(api, api.a.admin_token, GOOD)
    assert reply.status_code == 200, reply.text
    result = reply.json()
    assert result["applied"] is False
    assert [(r["line"], r["action"]) for r in result["rows"]] == [
        (2, "unchanged"),
        (3, "add"),
        (4, "add"),
    ]
    assert (result["added"], result["unchanged"], result["errors"]) == (2, 1, 0)
    assert len(await _chain(api)) == before
    async with tenant_tx(api.db.app, api.a.id) as conn:
        emails = set((await conn.execute(select(users.c.email))).scalars())
    assert "bob@a.example.test" not in emails


async def test_apply_is_idempotent(api):
    first = await _import(api, api.a.admin_token, GOOD, dry_run=False)
    assert first.status_code == 200, first.text
    assert first.json()["applied"] is True and first.json()["added"] == 3
    chain = await _chain(api)
    events = [r.event for r in chain]
    assert events.count("role.assigned") == 3 and events[-1] == "rbac.imported"
    assert "user.handles_changed" in events
    listed = await api.get("/v1/rbac/assignments", api.a.admin_token)
    held = {(a["email"], a["role"], a["team"]) for a in listed.json()}
    assert ("bob@a.example.test", "approver", "payments") in held
    assert ("cat@a.example.test", "auditor", None) in held
    again = await _import(api, api.a.admin_token, GOOD, dry_run=False)
    assert again.status_code == 200
    assert (again.json()["added"], again.json()["unchanged"]) == (0, 3)
    assert len(await _chain(api)) == len(chain)  # nothing changed, nothing recorded


async def test_one_bad_row_refuses_the_whole_file(api):
    text = GOOD + "dan@a.example.test,no-such-role,payments,\n"
    dry = await _import(api, api.a.admin_token, text)
    assert dry.status_code == 200 and dry.json()["errors"] == 1
    assert dry.json()["rows"][-1] == {
        "line": 5,
        "email": "dan@a.example.test",
        "role": "no-such-role",
        "team": "payments",
        "handle": None,
        "action": "error",
        "error": "no role 'no-such-role' in this tenant",
    }
    applied = await _import(api, api.a.admin_token, text, dry_run=False)
    assert applied.status_code == 422 and applied.json()["code"] == "rbac.import_invalid"
    assert applied.json()["problems"] == ["line 5: no role 'no-such-role' in this tenant"]
    listed = await api.get("/v1/rbac/assignments", api.a.admin_token)
    assert {a["email"] for a in listed.json()} == {"admin@a.example.test"}


async def test_a_team_lead_imports_only_into_their_team(api):
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    text = (
        "email,role,team,handle\n"
        "ann@a.example.test,developer,payments,\n"
        "bob@a.example.test,developer,search,\n"
        "cat@a.example.test,auditor,,\n"
        "dan@a.example.test,developer,payments,git.example.test:dan\n"
    )
    result = (await _import(api, lead, text)).json()
    actions = {row["line"]: (row["action"], row["error"]) for row in result["rows"]}
    assert actions[2] == ("add", None)
    assert actions[3][0] == "error" and "search" in actions[3][1]
    assert actions[4][0] == "error" and "rbac.assign" in actions[4][1]
    assert actions[5][0] == "error" and "handle" in actions[5][1]


async def test_people_without_assign_rights_cannot_import(api):
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    reply = await _import(api, dev, GOOD)
    assert reply.status_code == 403 and reply.json()["code"] == "rbac.forbidden"
    service = await api.post(
        "/v1/service-accounts", api.a.admin_token, {"name": "ci", "team": "payments", "days": 9}
    )
    reply = await _import(api, service.json()["token"], GOOD)
    assert reply.status_code == 403


async def test_an_import_in_one_tenant_never_touches_another(api):
    await _import(api, api.a.admin_token, GOOD, dry_run=False)
    listed = await api.get("/v1/rbac/assignments", api.b.admin_token)
    assert {a["email"] for a in listed.json()} == {"admin@b.example.test"}
    dry = (await _import(api, api.b.admin_token, GOOD)).json()
    assert [row["action"] for row in dry["rows"]] == ["add", "add", "add"]
