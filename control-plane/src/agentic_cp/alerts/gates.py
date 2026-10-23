"""Gate-waiting mail (M2-07, ruling P4): when an ingested batch holds a `gate_waiting`
event, the run's starter is mailed once per wait (the dedupe key names the event's seq).
The event names the gate but not its approver role (Zyroflo's `gate_waiting` fields are
`gate` and `hard`), so mailing the role's holders waits for the role to reach the control
plane (a deferred item)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.alerts.sinks import active_person
from agentic_cp.notify.outbox import SYSTEM, enqueue
from agentic_cp.notify.templates import render
from agentic_cp.tenancy.tenants import current_tenant


async def gate_waiting(conn: AsyncConnection, run, entries: Iterable[Mapping], *, clock) -> None:
    waits = [entry for entry in entries if entry.get("event") == "gate_waiting"]
    if not waits:
        return
    starter = await active_person(conn, run.started_by)
    if starter is None:  # a service account's run, or an offboarded starter
        return
    tenant = await current_tenant(conn)
    for entry in waits:
        gate = str(entry.get("gate") or entry.get("step") or "a gate")
        rendered = render(
            "gate.waiting",
            {"run_id": run.run_id, "team": run.team, "gate": gate, "role": "its approver"},
            tenant_name=tenant.name,
        )
        await enqueue(
            conn,
            kind="gate.waiting",
            recipient=starter,
            rendered=rendered,
            dedupe_key=f"gate:{run.run_id}:{entry.get('seq')}:{starter}",
            actor=SYSTEM,
            clock=clock,
            team=run.team,
        )
