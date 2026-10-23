"""The publish guard (M2-49): secrets and personal data in run events are masked before an
event is chained, or - once it is chained and can no longer change - refused at the sink.
Every hit is audited by kind and count, never by value."""

import logging
from pathlib import Path

import pytest

from agentic.policy import load_policy
from agentic.policy.model import PublishScanPolicy
from agentic.security.pii import PiiDetector
from agentic.security.publish_scan import PublishBlocked, PublishGuard, PublishScanEvent
from agentic.security.secret_scan import SecretScanner
from agentic.telemetry.sinks import SinkError

AWS = "AKIA" + "Z7Q4R2XW" + "9PLM3TKB"
CARD = "4111" + "1111" + "1111" + "1111"
EMAIL = "jane" + "@" + "example.com"
FIXTURES = Path(__file__).parents[1] / "fixtures"


def guard(action="mask", **kwargs):
    seen: list[PublishScanEvent] = []
    made = PublishGuard(
        SecretScanner(), PiiDetector(), action=action, on_event=seen.append, **kwargs
    )
    return made, seen


def entry(seq: int, **fields) -> dict:
    return {
        "schema_version": 1,
        "run_id": "r-1",
        "seq": seq,
        "at": "2026-10-23T10:00:00Z",
        "event": "step_finished",
        "step": "develop",
        "prev": "",
        "hash": "f" * 64,
        **fields,
    }


def test_screen_masks_secret_and_pii_as_ids():
    made, seen = guard()
    fields = {"outcome": f"order-{CARD}", "artifacts": {f"notes/{AWS}.md": "sha256:" + "a" * 64}}
    screened = made.screen("step_finished", fields)
    assert screened["outcome"] == "order-masked-credit-card"
    # the secret scanner's mask runs on over token characters (M1-41), the suffix too
    assert list(screened["artifacts"]) == ["notes/masked-secret"]
    assert fields["outcome"] == f"order-{CARD}"  # the caller's dict is not changed
    (event,) = seen
    assert event.action == "masked" and event.where == "event:step_finished"
    assert event.findings == {"AWSKeyDetector": 1, "pii:credit-card": 1}
    assert event.fields_hit == ("artifacts", "outcome")


def test_clean_fields_are_returned_as_they_are_and_not_audited():
    made, seen = guard()
    fields = {"outcome": "completed", "cost": {"usd": "0.12"}}
    assert made.screen("step_finished", fields) is fields
    assert seen == []


def test_screen_under_block_leaves_fields_for_the_sink():
    made, seen = guard("block")
    fields = {"outcome": CARD}
    assert made.screen("step_finished", fields) is fields
    assert seen == []


def test_identity_ledger_and_structural_fields_are_not_scanned():
    made, seen = guard()
    agent = object()  # the ledger's AgentIdentity rides the same call
    fields = {"approver": EMAIL, "actor": EMAIL, "agent": agent, "skills": ["personal/x"]}
    assert made.screen("gate_decided", fields) is fields
    made.check("r-1", [entry(1, approver=EMAIL, run_id="r-1")])
    assert seen == []


def test_identity_fields_come_from_policy():
    made, _ = guard(identity_fields=[])
    assert made.screen("gate_decided", {"approver": EMAIL})["approver"] == "masked-email"


def test_check_raises_publish_blocked_naming_seq_and_kinds_never_values():
    made, seen = guard()
    with pytest.raises(PublishBlocked) as caught:
        made.check("r-1", [entry(1, outcome="completed"), entry(2, outcome=f"x-{CARD}")])
    message = str(caught.value)
    assert isinstance(caught.value, SinkError)
    assert "event 2" in message and "credit-card" in message and CARD not in message
    assert "nothing was sent" in message
    (event,) = seen
    assert (event.action, event.run_id, event.seq) == ("blocked", "r-1", 2)
    assert CARD not in repr(event.fields())


def test_check_passes_clean_entries():
    made, seen = guard()
    made.check("r-1", [entry(1, outcome="completed"), entry(2, gate="review", approver=EMAIL)])
    assert seen == []


def test_unscannable_text_fails_closed(monkeypatch):
    made, _ = guard()

    def slow(text, deadline=None):
        raise TimeoutError

    monkeypatch.setattr(made.scanner, "findings", slow)
    assert made.screen("step_finished", {"outcome": "x"})["outcome"] == "masked-unscannable"


def test_default_audit_goes_to_the_audit_logger(caplog):
    made = PublishGuard(SecretScanner(), PiiDetector())
    with caplog.at_level(logging.WARNING, logger="agentic.audit"):
        made.screen("step_finished", {"outcome": CARD})
    (record,) = caplog.records
    assert record.audit["event"] == "publish_scan" and CARD not in record.getMessage()


def test_policy_defaults_and_validation():
    assert PublishScanPolicy().action == "mask"
    assert PublishScanPolicy().identity_fields == ["approver", "owner", "actor"]
    with pytest.raises(ValueError, match="unknown PII entities"):
        PublishScanPolicy(pii=["passport"])
    with pytest.raises(ValueError, match="empty"):
        PublishScanPolicy(pii=[])


def test_policy_loads_from_org_yaml_and_builds_the_guard(tmp_path):
    import shutil

    root = tmp_path / "policy"
    shutil.copytree(FIXTURES / "policy" / "policy", root)
    org = root / "org.yaml"
    text = org.read_text(encoding="utf-8") if org.is_file() else ""
    org.write_text(text + "publish_scan:\n  action: block\n  pii: [email]\n", encoding="utf-8")
    policy = load_policy(root)
    assert policy.publish_scan.action == "block"
    made = PublishGuard.from_policy(policy)
    assert made.action == "block" and made.pii.entities == ("email",)
