"""Personal skill and note backups (M2-20; Review Focus 5): each user's laptops send their
personal files as chained events, per device; replays are no-ops, a gap or a fork is 409
with the head, a broken chain 422; the newest copy of each file is read back by its owner
only - never by another user of the tenant, an org admin included - and every event is on
the tenant's audit chain without its content."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import pytest
from apiclient import member
from sqlalchemy import select

from agentic_cp.audit import read_records
from agentic_cp.tables import personal_events
from agentic_cp.tenancy.context import tenant_tx
from sdlc_commons.ledger import entry_hash

pytestmark = pytest.mark.anyio

DEVICE = uuid.uuid4().hex


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def chain(events: list[dict], start: tuple[int, str] = (0, "")) -> list[dict]:
    """Entries as the CLI's journal chains them (Zyroflo's entry hash)."""
    seq, prev = start
    out = []
    for event in events:
        seq += 1
        entry = {"seq": seq, "prev": prev, "at": f"2026-10-24T10:00:{seq:02d}+00:00", **event}
        entry["hash"] = entry_hash(entry)
        prev = entry["hash"]
        out.append(entry)
    return out


def saved(path: str, content: str, kind: str = "memory") -> dict:
    return {
        "event": "personal.saved",
        "kind": kind,
        "path": path,
        "sha256": sha(content),
        "content": content,
    }


def removed(path: str, kind: str = "memory") -> dict:
    return {"event": "personal.removed", "kind": kind, "path": path, "sha256": sha("")}


def held(path: str, content: str) -> dict:
    return {
        "event": "personal.held",
        "kind": "memory",
        "path": path,
        "sha256": sha(content),
        "findings": {"pii:EMAIL_ADDRESS": 1},
    }


async def send(api, token, entries, device=DEVICE):
    return await api.post(f"/v1/personal/devices/{device}/events", token, {"entries": entries})


async def files(api, token) -> dict[str, dict]:
    reply = await api.get("/v1/personal/files", token)
    assert reply.status_code == 200, reply.text
    return {item["path"]: item for item in reply.json()}


@pytest.fixture
async def dev(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    return token


async def test_events_are_stored_and_replays_are_no_ops(api, dev):
    entries = chain([saved("memory/notes.md", "use uv"), saved("memory/more.md", "be brief")])
    first = await send(api, dev, entries)
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 2 and first.json()["duplicates"] == 0
    again = await send(api, dev, entries)
    assert again.json() == {**first.json(), "accepted": 0, "duplicates": 2}
    found = await files(api, dev)
    assert found["memory/notes.md"]["content"] == "use uv"
    assert found["memory/notes.md"]["device"] == DEVICE


async def test_a_gap_or_a_fork_is_409_with_the_head(api, dev):
    entries = chain([saved("memory/a.md", "a"), saved("memory/b.md", "b")])
    gap = await send(api, dev, entries[1:])
    assert gap.status_code == 409 and gap.json()["code"] == "ingest.gap"
    assert gap.json()["head"] == {"seq": 0, "hash": ""}
    assert (await send(api, dev, entries[:1])).status_code == 200
    fork = chain([saved("memory/a.md", "something else")])
    refused = await send(api, dev, fork)
    assert refused.status_code == 409 and refused.json()["code"] == "ingest.fork"
    assert refused.json()["head"]["seq"] == 1
    # review F3: the hints speak of the device's backup chain, not of a run
    assert "run" not in gap.json()["hint"] and "device" in gap.json()["hint"]
    assert "run" not in refused.json()["hint"] and "new device" in refused.json()["hint"]


async def test_a_broken_chain_or_entry_is_422(api, dev):
    (good,) = chain([saved("memory/a.md", "a")])
    tampered = {**good, "content": "b", "sha256": sha("b")}  # hash no longer matches
    for bad in (
        [tampered],
        chain([{**saved("memory/a.md", "a"), "sha256": sha("not a")}]),  # content vs sha256
        chain([saved("../.ssh/id_rsa", "x")]),
        chain([saved("skills/x/run.sh", "x", kind="skill")]),
        chain([{**saved("memory/a.md", "a"), "extra": 1}]),
        chain([{**removed("memory/a.md"), "content": "left"}]),
        chain([{**saved("memory/a.md", "a"), "event": "personal.other"}]),
    ):
        reply = await send(api, dev, bad)
        assert reply.status_code == 422, (bad, reply.text)
        assert reply.json()["code"] == "ingest.chain_broken"


async def test_other_users_never_see_content(api, dev):
    await send(api, dev, chain([saved("memory/private.md", "my own note")]))
    other, _ = await member(api, api.a, "other@a.example.test", "developer", "payments")
    assert await files(api, other) == {}
    assert await files(api, api.a.admin_token) == {}  # an org admin neither
    assert await files(api, api.b.admin_token) == {}
    # another user's chain on the same device id is a different chain
    mine = await send(api, other, chain([saved("memory/private.md", "theirs")]))
    assert mine.status_code == 200 and mine.json()["accepted"] == 1
    assert (await files(api, dev))["memory/private.md"]["content"] == "my own note"


async def test_each_event_is_audited_without_content(api, dev):
    await send(
        api,
        dev,
        chain(
            [
                saved("memory/a.md", "secret-free text"),
                held("memory/b.md", "mail me"),
                removed("memory/a.md"),
            ]
        ),
    )
    async with tenant_tx(api.db.app, api.a.id) as conn:
        records = [r for r in await read_records(conn) if r.event.startswith("personal.")]
    assert [r.event for r in records] == ["personal.saved", "personal.held", "personal.removed"]
    assert all(r.actor == "dev@a.example.test" for r in records)
    assert records[0].target == "personal:memory/a.md"
    assert records[0].after["sha256"] == sha("secret-free text")
    assert records[1].after["findings"] == {"pii:EMAIL_ADDRESS": 1}
    assert "secret-free text" not in str([r.entry() for r in records])


async def test_the_newest_copy_wins_across_devices(api, dev):
    other_laptop = uuid.uuid4().hex
    first = chain([saved("memory/n.md", "old")])
    await send(api, dev, first)
    api.clock.advance(timedelta(minutes=1))
    await send(api, dev, chain([saved("memory/n.md", "new")]), device=other_laptop)
    assert (await files(api, dev))["memory/n.md"]["content"] == "new"
    api.clock.advance(timedelta(minutes=1))
    later = chain([removed("memory/n.md")], start=(1, first[0]["hash"]))
    assert (await send(api, dev, later)).status_code == 200
    gone = (await files(api, dev))["memory/n.md"]
    assert gone["deleted"] is True and gone["device"] == DEVICE


async def test_a_removed_or_held_file_is_marked(api, dev):
    entries = chain(
        [
            saved("memory/gone.md", "x"),
            removed("memory/gone.md"),
            saved("memory/kept.md", "v1"),
            held("memory/kept.md", "v2 with a mail address"),
        ]
    )
    assert (await send(api, dev, entries)).status_code == 200
    found = await files(api, dev)
    assert found["memory/gone.md"]["deleted"] is True and found["memory/gone.md"]["content"] is None
    kept = found["memory/kept.md"]
    assert kept["content"] == "v1" and kept["held"] is True  # the newer version stayed local


async def test_service_accounts_and_auditors_cannot_back_up(api):
    made = await api.post(
        "/v1/service-accounts",
        api.a.admin_token,
        {"name": "ci-bot", "team": "payments", "days": 30},
    )
    assert made.status_code == 201, made.text
    robot = made.json()["token"]
    auditor, _ = await member(api, api.a, "auditor@a.example.test", "auditor")
    for token in (robot, auditor):
        refused = await send(api, token, chain([saved("memory/a.md", "a")]))
        assert refused.status_code == 403, refused.text
        assert (await api.get("/v1/personal/files", token)).status_code == 403


async def test_a_device_must_be_an_id(api, dev):
    reply = await send(api, dev, chain([saved("memory/a.md", "a")]), device="not-a-device")
    assert reply.status_code == 422


async def test_rows_are_the_owners(api, dev):
    await send(api, dev, chain([saved("memory/a.md", "a")]))
    async with tenant_tx(api.db.app, api.b.id) as conn:
        assert list(await conn.execute(select(personal_events))) == []


# ── bounds (review F4): personal_events is insert-only, so what one user stores is capped ──


def _quota_refusal(reply) -> None:
    assert reply.status_code == 429, reply.text
    assert reply.json()["code"] == "personal.quota" and reply.json()["hint"]


async def test_a_user_backs_up_from_a_bounded_number_of_devices(api, dev):
    capped = api.with_settings(personal_max_devices=2)
    first = chain([saved("memory/a.md", "a")])
    for _ in range(2):
        laptop = uuid.uuid4().hex
        reply = await send(capped, dev, first, device=laptop)
        assert reply.status_code == 200, reply.text
    third = await send(capped, dev, first, device=uuid.uuid4().hex)
    _quota_refusal(third)
    assert "device" in third.json()["detail"]
    # a known device keeps backing up, and another user has their own devices
    more = chain([saved("memory/b.md", "b")], start=(1, first[0]["hash"]))
    assert (await send(capped, dev, more, device=laptop)).status_code == 200
    other, _ = await member(api, api.a, "other@a.example.test", "developer", "payments")
    assert (await send(capped, other, chain([saved("memory/a.md", "a")]))).status_code == 200


async def test_what_a_user_stores_is_capped(api, dev):
    capped = api.with_settings(personal_max_bytes=4000)
    first = chain([saved("memory/a.md", "x" * 1000)])  # 1000 bytes + 1 KiB per event
    assert (await send(capped, dev, first)).status_code == 200
    second = chain([saved("memory/b.md", "y" * 1000)], start=(1, first[0]["hash"]))
    refused = await send(capped, dev, second)
    _quota_refusal(refused)
    assert set(await files(api, dev)) == {"memory/a.md"}
    # the cap counts every device of the user, and a replay of stored events is still fine
    elsewhere = chain([saved("memory/c.md", "z" * 1000)])
    _quota_refusal(await send(capped, dev, elsewhere, device=uuid.uuid4().hex))
    assert (await send(capped, dev, first)).json()["duplicates"] == 1


async def test_what_a_user_sends_in_a_day_is_capped(api, dev):
    capped = api.with_settings(personal_daily_bytes=4000)
    first = chain([saved("memory/a.md", "x" * 1000)])
    assert (await send(capped, dev, first)).status_code == 200
    second = chain([saved("memory/b.md", "y" * 1000)], start=(1, first[0]["hash"]))
    _quota_refusal(await send(capped, dev, second))
    api.clock.advance(timedelta(hours=25))
    assert (await send(capped, dev, second)).status_code == 200


async def test_the_files_list_comes_in_pages(api, dev):
    names = [f"memory/n{n}.md" for n in range(5)]
    assert (await send(api, dev, chain([saved(name, name) for name in names]))).is_success
    first = await api.get("/v1/personal/files?limit=2", dev)
    assert [f["path"] for f in first.json()] == names[:2]
    after = names[1]
    second = await api.get(f"/v1/personal/files?limit=2&after={after}", dev)
    assert [f["path"] for f in second.json()] == names[2:4]
    last = await api.get(f"/v1/personal/files?limit=2&after={names[3]}", dev)
    assert [f["path"] for f in last.json()] == names[4:]
    assert (await api.get("/v1/personal/files?limit=100000", dev)).status_code == 422
