"""The publish scan in the CLI (M2-49): in org mode a run's events are screened before the
ledger chains them, so planted personal data reaches the control plane masked and the
chain still verifies there; an entry chained with a hit is never sent."""

from __future__ import annotations

import json
import logging

import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic import auth
from agentic_cli import control_plane
from agentic_cli.config import agentic_home
from agentic_cli.control_plane import CONTROL_PLANE_TOKEN, record_login
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main
from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace
from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import RunLedger, verify_entries

CARD = "4111" + "1111" + "1111" + "1111"


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


@pytest.fixture
def fake():
    return FakeControlPlane()


@pytest.fixture
def org(home, repo, policy_repo, monkeypatch, fake):
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    for name in ("AGENTIC_CONTROL_PLANE_URL", "AGENTIC_CONTROL_PLANE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTIC_POLICY_REPO", str(policy_repo))
    config = agentic_home()
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        f"org: acme\ncontrol_plane_url: {CP_URL}\nteam: payments\n", encoding="utf-8"
    )
    auth.Credentials().store(CONTROL_PLANE_TOKEN, TOKEN)
    record_login(config, CP_URL, "acme", "dev@acme.example")
    return repo


def org_yaml(policy_repo, extra: str) -> None:
    org = policy_repo / "policy" / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + extra, encoding="utf-8")


def _record(repo, run_id: str, **fields) -> Runtime:
    rt = Runtime(Workspace.load(repo, home=agentic_home()))
    recorder = rt.forwarding(run_id)
    recorder.record("step_finished", "develop", **fields)
    rt.close_sinks()
    return rt


def test_planted_pii_is_masked_before_ingest_and_chain_verifies(org, fake, caplog):
    with caplog.at_level(logging.WARNING, logger="agentic.audit"):
        _record(org, "run-pii", outcome=f"order-{CARD}")
    sent = fake.events["run-pii"]
    assert sent[-1]["outcome"] == "order-masked-credit-card"
    assert CARD not in json.dumps(sent)
    assert verify_entries(sent, run_id="run-pii") == []
    audit = [r.audit for r in caplog.records if getattr(r, "audit", None)]
    assert {"event": "publish_scan", "action": "masked"}.items() <= audit[0].items()
    # the local ledger holds the same chain: what was chained is what was published
    local = RunLedger(org, "run-pii").events()
    assert [e["hash"] for e in local] == [e["hash"] for e in sent]


def test_unmaskable_chained_pii_blocks_delivery(org, fake, monkeypatch, capsys):
    rt = Runtime(Workspace.load(org, home=agentic_home()))
    ledger = rt.recorder("run-chained")  # written past the screen: already chained
    ledger.record("step_finished", phase="develop", outcome=f"order-{CARD}")
    ledger.release()
    monkeypatch.chdir(org)
    assert main(["--json", "sync", "run-chained"]) == ExitCode.ERROR
    result = json.loads(capsys.readouterr().out)["runs"]["run-chained"]
    assert result["sent"] == 0 and result["queued"] == 2
    assert "credit-card" in result["error"] and CARD not in result["error"]
    assert "run-chained" not in fake.events


def test_block_policy_keeps_pii_local(org, fake, policy_repo):
    org_yaml(policy_repo, "publish_scan:\n  action: block\n")
    _record(org, "run-block", outcome=f"order-{CARD}")
    local = RunLedger(org, "run-block").events()
    assert local[-1]["outcome"] == f"order-{CARD}"  # block never rewrites the local record
    published = fake.events.get("run-block", [])
    assert len(published) < len(local) and CARD not in json.dumps(published)


def test_clean_events_are_published_unchanged(org, fake):
    _record(org, "run-clean", outcome="completed")
    assert fake.events["run-clean"][-1]["outcome"] == "completed"
