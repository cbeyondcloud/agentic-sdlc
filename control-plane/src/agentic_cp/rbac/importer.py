"""Bulk role import from CSV (M2-14, ruling P7).

`parse` reads the file's text - a UTF-8 byte order mark dropped, LF or CRLF, quoted fields
(a field may hold a line break) - into rows that each know the physical line they start
on, and reports every malformed row by that line. The header names the columns:
`email` and `role` are required, `team` and `handle` optional, in any order and case.

`plan_import` asks, row by row, exactly what a single assignment asks
(`rbac.api.plan_assignment`), so a team lead's row for another team is an error like the
single call would be; setting a handle needs org `rbac.assign` (ND-6). `apply_import`
writes the rows in line order, re-planning each against what the earlier rows wrote; it is
additive (an assignment the file does not list is never removed) and the caller refuses the
whole file when any row errs."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.orgapi import ImportRow
from agentic.orgapi.models import HANDLE, ROLE_NAME, TEAM_SLUG
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.tokens import TokenContext
from agentic_cp.auth.users import UserError, normalise_email
from agentic_cp.errors import ApiProblem
from agentic_cp.policy_sync.service import Actor, active_teams
from agentic_cp.rbac import repos
from agentic_cp.rbac.api import apply_assignment, plan_assignment
from agentic_cp.rbac.authz import Org, authorize
from agentic_cp.tables import users

COLUMNS = ("email", "role", "team", "handle")
REQUIRED = ("email", "role")
MAX_ROWS = 5000
#: The import's own reason on each `role.assigned` record.
REASON = "csv import"


@dataclass(frozen=True)
class CsvRow:
    line: int
    email: str
    role: str
    team: str | None
    handle: str | None


def _error(line: int, message: str, **values) -> ImportRow:
    return ImportRow(line=line, action="error", error=message, **values)


def parse(text: str) -> tuple[list[CsvRow], list[ImportRow]]:
    """The well-formed rows and an error per malformed row (or for the header)."""
    reader = csv.reader(io.StringIO(text.removeprefix("﻿"), newline=""))
    try:
        header = next(reader)
    except StopIteration:
        return [], [_error(1, "the file is empty: its first line must be the header email,role")]
    except csv.Error as error:
        return [], [_error(1, f"the header is not CSV: {error}")]
    names = [name.strip().lower() for name in header]
    unknown = [name for name in names if name not in COLUMNS]
    missing = [name for name in REQUIRED if name not in names]
    if unknown or missing or len(set(names)) != len(names):
        problem = (
            f"unknown column(s) {', '.join(unknown)}"
            if unknown
            else f"missing column(s) {', '.join(missing)}"
            if missing
            else "a column is named twice"
        )
        return [], [_error(1, f"header: {problem} (the columns are {', '.join(COLUMNS)})")]
    rows: list[CsvRow] = []
    errors: list[ImportRow] = []
    seen: dict[tuple[str, str, str | None], int] = {}
    start = reader.line_num + 1
    while True:
        line = start
        try:
            record = next(reader)
        except StopIteration:
            break
        except csv.Error as error:
            errors.append(_error(line, f"not CSV: {error}"))
            break
        start = reader.line_num + 1
        if not any(cell.strip() for cell in record):
            continue  # a blank line
        if len(rows) + len(errors) >= MAX_ROWS:
            errors.append(_error(line, f"a file holds at most {MAX_ROWS} rows"))
            break
        if len(record) > len(names):
            errors.append(_error(line, f"{len(record)} fields, but the header names {len(names)}"))
            continue
        values = {name: cell.strip() for name, cell in zip(names, record, strict=False)}
        shown = {
            "email": values.get("email", ""),
            "role": values.get("role", ""),
            "team": values.get("team") or None,
            "handle": values.get("handle") or None,
        }
        problem = _row_problem(shown)
        if problem is not None:
            errors.append(_error(line, problem, **shown))
            continue
        shown["email"] = normalise_email(shown["email"])
        shown["handle"] = shown["handle"].lower() if shown["handle"] else None
        key = (shown["email"], shown["role"], shown["team"])
        if key in seen:
            errors.append(_error(line, f"the same assignment as line {seen[key]}", **shown))
            continue
        seen[key] = line
        rows.append(CsvRow(line=line, **shown))
    return rows, errors


def _row_problem(values: dict) -> str | None:
    try:
        normalise_email(values["email"])
    except UserError:
        return "email: not an email address"
    if not re.fullmatch(ROLE_NAME, values["role"]):
        return "role: not a role name (lower-case letters, digits and '-')"
    if values["team"] is not None and not re.fullmatch(TEAM_SLUG, values["team"]):
        return "team: not a team name (lower-case letters, digits and '-')"
    if values["handle"] is not None and not re.fullmatch(HANDLE, values["handle"]):
        return "handle: not a host-qualified handle (for example github.com:alice)"
    return None


def _shown(row: CsvRow) -> dict:
    return {"email": row.email, "role": row.role, "team": row.team, "handle": row.handle}


async def _handles(conn: AsyncConnection, email: str) -> set[str]:
    found = await conn.execute(select(users.c.handles).where(users.c.email == email))
    held = found.scalar_one_or_none()
    return set(held or ())


async def plan_import(
    conn: AsyncConnection, ctx: TokenContext, cache, rows: list[CsvRow]
) -> list[ImportRow]:
    """What applying each row would do: `add`, `unchanged` or `error` (nothing written)."""
    teams = await active_teams(conn)
    org_wide = (await authorize(ctx, "rbac.assign", Org(), conn=conn, cache=cache)).allowed
    planned = []
    for row in rows:
        try:
            plan = await plan_assignment(
                conn, ctx, cache, row.email, row.role, row.team, teams=teams
            )
        except ApiProblem as problem:
            planned.append(_error(row.line, problem.detail, **_shown(row)))
            continue
        if row.handle and not org_wide:
            planned.append(
                _error(row.line, "setting a handle needs rbac.assign for the org", **_shown(row))
            )
            continue
        has_handle = row.handle is None or row.handle in await _handles(conn, row.email)
        action = "unchanged" if plan.exists and has_handle else "add"
        planned.append(ImportRow(line=row.line, action=action, **_shown(row)))
    return planned


async def apply_import(
    conn: AsyncConnection, ctx: TokenContext, state, rows: list[CsvRow], revision: int
) -> None:
    """Write every row in line order (the caller holds the RBAC lock, planned the rows
    without an error and bumped the revision to `revision`)."""
    teams = await active_teams(conn)
    handles_changed = False
    for row in rows:
        plan = await plan_assignment(
            conn, ctx, state.enforcers, row.email, row.role, row.team, teams=teams
        )
        if not plan.exists:
            await apply_assignment(conn, ctx, state, plan, revision=revision, reason=REASON)
        if row.handle:
            before = await _handles(conn, row.email)
            if row.handle not in before:
                after = sorted(before | {row.handle})
                await conn.execute(
                    update(users).where(users.c.email == row.email).values(handles=after)
                )
                await append(
                    conn,
                    AuditEvent(
                        event="user.handles_changed",
                        actor=ctx.email,
                        actor_kind=ctx.actor_kind,
                        target=f"user:{row.email}",
                        reason=REASON,
                        before={"handles": sorted(before)},
                        after={"handles": after, "rbac_revision": revision},
                    ),
                    clock=state.clock,
                )
                handles_changed = True
    if handles_changed:
        await repos.resync_all(conn, state.clock, Actor(ctx.email, ctx.actor_kind))
