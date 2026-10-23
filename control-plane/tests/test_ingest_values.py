"""Values ingest accepts or refuses before storing (DF-493, DF-494; M2-21): a real NUL is
refused but a string holding the six characters `\\u0000` is not, and floats that jsonb would
hand back as another number (exponent form, -0.0) are refused up front - stored, they would
never verify again."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from apiclient import member
from policy_repos import head, make_policy_repo, url

from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import RunIdentity, RunLedger, entry_hash, verify_entries

pytestmark = pytest.mark.anyio

POLICY = "git:" + "c" * 40


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


@pytest.fixture
async def dev(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    return token


def chain(tmp_path: Path, run_id: str, **cost) -> list[dict]:
    ledger = RunLedger(tmp_path / run_id, run_id)
    ledger.open(
        identity=RunIdentity(team="payments", policy_version=POLICY),
        kind="feature",
        project="demo",
        owner="payments",
        criticality="low",
    )
    if cost:
        ledger.record("step_finished", phase="requirements", attempt=1, outcome="pass", cost=cost)
    entries = ledger.events()
    ledger.release()
    return entries


async def post(api, token, entries, run_id):
    return await api.post(f"/v1/runs/{run_id}/events", token, {"entries": entries})


async def test_the_six_characters_backslash_u0000_are_not_a_nul(api, dev, tmp_path):
    entries = copy.deepcopy(chain(tmp_path, "run-esc"))
    entries[0]["project"] = "demo\\u0000"  # a backslash, a 'u' and four zeros
    entries[0]["hash"] = entry_hash(entries[0])
    reply = await post(api, dev, entries, "run-esc")
    assert reply.status_code == 200, reply.text
    stored = (await api.get("/v1/runs/run-esc/events", dev)).json()
    assert stored == entries and verify_entries(stored, run_id="run-esc") == []


async def test_a_real_nul_in_a_key_or_value_is_refused(api, dev, tmp_path):
    for n, (key, value) in enumerate((("project", "demo\u0000"), ("note\u0000", "x"))):
        entries = copy.deepcopy(chain(tmp_path, f"run-nul-{n}"))
        entries[0][key] = value
        entries[0]["hash"] = entry_hash(entries[0])
        reply = await post(api, dev, entries, f"run-nul-{n}")
        assert reply.status_code == 422 and "NUL" in reply.text


@pytest.mark.parametrize("value", [1e16, 1.5e22, -0.0])
async def test_floats_jsonb_would_change_are_refused(api, dev, tmp_path, value):
    entries = chain(tmp_path, "run-float", amount=value)
    reply = await post(api, dev, entries, "run-float")
    assert reply.status_code == 422, reply.text
    assert "jsonb" in reply.json()["detail"]


async def test_policy_known_is_recomputed_while_false(api, dev, tmp_path):
    """DF-488: a run whose policy version the tenant accepts after the run opened is no
    longer flagged once its next batch arrives."""
    repo = make_policy_repo(tmp_path / "policy")
    version = "git:" + head(repo)
    ledger = RunLedger(tmp_path / "late", "run-late")
    ledger.open(
        identity=RunIdentity(team="payments", policy_version=version),
        kind="feature",
        project="demo",
        owner="payments",
        criticality="low",
    )
    ledger.record("step_started", phase="requirements", attempt=1)
    entries = ledger.events()
    ledger.release()
    assert (await post(api, dev, entries[:1], "run-late")).status_code == 200
    assert (await api.get("/v1/runs/run-late", dev)).json()["policy_known"] is False
    source = {"repo": url(repo), "branch": "main"}
    assert (await api.put("/v1/tenant/policy-source", api.a.admin_token, source)).status_code == 200
    assert (await post(api, dev, entries[1:], "run-late")).status_code == 200
    assert (await api.get("/v1/runs/run-late", dev)).json()["policy_known"] is True


async def test_ordinary_floats_still_pass(api, dev, tmp_path):
    entries = chain(tmp_path, "run-ok", usd=0.125, tiny=1e-07, whole=3.0)
    assert (await post(api, dev, entries, "run-ok")).status_code == 200
    stored = (await api.get("/v1/runs/run-ok/events", dev)).json()
    assert verify_entries(stored, run_id="run-ok") == []
