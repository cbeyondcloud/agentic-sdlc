"""Gate-waiting mail (M2-07, ruling P4): when an ingested batch holds a `gate_waiting`
event, the people who can act are mailed once per wait (the dedupe key names the event's
seq and the recipient): the active people assigned the gate's approver role in the run's
team - the engine records that role in our ext block (`approver_role`, review F5) - and
the run's starter, at most `MAX_RECIPIENTS` in all. A pause that is not a pipeline gate
(a batch or an invalid answer) names no role and mails the starter only."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.alerts.sinks import MAX_RECIPIENTS, active_person, people_holding
from agentic_cp.notify.outbox import SYSTEM, enqueue
from agentic_cp.notify.templates import render
from agentic_cp.tenancy.tenants import current_tenant
from sdlc_commons.ledger import NAMESPACE

#: How the mail names the deciding person when the wait names no role.
NO_ROLE = "a person who may decide it"


def approver_role(entry: Mapping) -> str | None:
    """The approver role a `gate_waiting` entry names (verified against our declared ext
    fields at ingest), or None."""
    ext = entry.get("ext")
    block = ext.get(NAMESPACE) if isinstance(ext, Mapping) else None
    role = block.get("approver_role") if isinstance(block, Mapping) else None
    return role if isinstance(role, str) and role else None


async def gate_waiting(conn: AsyncConnection, run, entries: Iterable[Mapping], *, clock) -> None:
    waits = [entry for entry in entries if entry.get("event") == "gate_waiting"]
    if not waits:
        return
    starter = await active_person(conn, run.started_by)  # None: a service account's run
    tenant = await current_tenant(conn)
    for entry in waits:
        role = approver_role(entry)
        holders = await people_holding(conn, run.team, role=role) if role else []
        recipients = list(dict.fromkeys([*([starter] if starter else []), *holders]))
        gate = str(entry.get("gate") or entry.get("step") or "a gate")
        rendered = render(
            "gate.waiting",
            {"run_id": run.run_id, "team": run.team, "gate": gate, "role": role or NO_ROLE},
            tenant_name=tenant.name,
        )
        for email in recipients[:MAX_RECIPIENTS]:
            await enqueue(
                conn,
                kind="gate.waiting",
                recipient=email,
                rendered=rendered,
                dedupe_key=f"gate:{run.run_id}:{entry.get('seq')}:{email}",
                actor=SYSTEM,
                clock=clock,
                team=run.team,
            )
