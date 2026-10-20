"""Tests for the secret scanner: what is found, what is masked, what is recorded (M1-41).

Planted secrets are assembled from pieces, so this file never holds one whole.
"""

import json
import logging
import re
import time

import pytest

from agentic.security import (
    DEFAULT_DETECTORS,
    SecretBlocked,
    SecretScanEvent,
    SecretScanner,
    known_detectors,
)

AWS = "AKIA" + "Z7Q4R2XW" + "9PLM3TKB"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" + "k1L2m3N4o5P6" + "q7R8"
SLACK = "xoxb-" + "123456789012-" + "abcdefghij"
PEM_BODY = "MIIEowIBAAKCAQEAx4f0Zq1\nb3BlbnNzaC1rZXktdjEAAAA\nQyNTUxOQAAACBhc2Rm"
PEM = "-----" + "BEGIN RSA PRIVATE KEY-----\n" + PEM_BODY + "\n-----END RSA PRIVATE KEY-----"
CODE = "def handler(event):\n    return {'status': 200, 'sha': '3f786850e387550fdab836ed7e6d'}\n"


def events() -> tuple[list[SecretScanEvent], SecretScanner, SecretScanner]:
    seen: list[SecretScanEvent] = []
    return seen, SecretScanner(on_event=seen.append), SecretScanner("mask", on_event=seen.append)


# ── finding ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("secret", "kind"),
    [
        (AWS, "AWS Access Key"),
        (GITHUB, "GitHub Token"),
        (SLACK, "Slack Token"),
        (PEM, "Private Key"),
    ],
)
def test_planted_secrets_are_found(secret, kind):
    found = SecretScanner().findings(f"config = '{secret}'\n")
    assert kind in {f.kind for f in found}


def test_ordinary_code_has_no_findings():
    assert SecretScanner().findings(CODE) == []


# review F1: key formats detect-secrets 1.5.0 does not know, and keyword assignments
KEY_TAIL = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0" * 2  # 80 token characters
CURRENT_KEYS = {
    "AnthropicKeyDetector": "sk-ant-" + "api03-" + KEY_TAIL,
    "OpenAIProjectKeyDetector": "sk-" + "proj-" + KEY_TAIL,
    "GoogleApiKeyDetector": "AIza" + "SyA-1234567890abcdefghijklmnopqrstuv",
    "GitHubFineGrainedTokenDetector": "github_" + "pat_11ABCDEFG0" + KEY_TAIL[:60],
    "HuggingFaceTokenDetector": "hf_" + KEY_TAIL[:34],
}


@pytest.mark.parametrize("detector", sorted(CURRENT_KEYS))
def test_current_provider_key_formats_are_found_and_masked_whole(detector):
    secret = CURRENT_KEYS[detector]
    masked, counts = SecretScanner("mask").mask(f"export KEY={secret}\n")
    assert counts == {detector: 1}
    assert secret[8:] not in masked and masked.startswith("export KEY=[MASKED SECRET: ")


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('password = "' + "SuperSecret" + 'Value123!"', "SuperSecretValue123!"),
        ("DB_PASSWORD=" + "hunter2x", "hunter2x"),
        ('{"client_secret": "' + "q8Zr" + 'T4vX9mLp"}', "q8ZrT4vX9mLp"),
    ],
)
def test_a_password_assignment_masks_only_its_value(text, value):
    masked, counts = SecretScanner("mask").mask(text)
    assert counts == {"PasswordAssignmentDetector": 1}
    assert value not in masked
    assert "[MASKED SECRET: Password Assignment]" in masked


@pytest.mark.parametrize(
    "code",
    [
        'password = os.environ["DB_PASSWORD"]',
        "api_key = settings.api_key",
        'password = "${DB_PASSWORD}"',
        'password: "{{ vault_db_password }}"',
        'api_key = "your-api-key-here"',
        "DB_PASSWORD=$DB_PASSWORD",
        "PWD=/home/user1/app",
        "PASSWORD_PATTERN = re.compile(r'pw[0-9]+')",  # a code constant, not an env file
        "CERT_ACCESS_KEY = 0x0001",
        'client_secret="your-auth0-client-secret"',
        'client_secret="abc123..."',
    ],
)
def test_code_that_names_a_password_without_one_is_not_a_finding(code):
    assert SecretScanner().findings(code) == []


def test_the_assignment_rules_stay_linear_on_long_runs():
    scanner = SecretScanner(detectors=["PasswordAssignmentDetector"])
    start = time.perf_counter()
    scanner.findings("password-" * 60_000)
    scanner.findings("MY_API_KEY_" * 40_000 + "=\n")
    assert time.perf_counter() - start < 3.0


# review F2(b): documented example values and policy allow values are not secrets
AWS_EXAMPLE_ID = "AKIA" + "IOSFODNN7" + "EXAMPLE"
AWS_EXAMPLE_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"


def test_documented_example_keys_are_not_secrets():
    text = f"aws_access_key_id = {AWS_EXAMPLE_ID}\naws_secret_access_key = {AWS_EXAMPLE_SECRET}\n"
    assert SecretScanner().findings(text) == []
    assert SecretScanner().check_text(text, "model-call") == text


def test_allowed_values_are_not_secrets_but_others_still_are():
    fixture = "AKIA" + "TESTFIXTURE0" + "0001"
    scanner = SecretScanner(allow=[fixture])
    assert scanner.findings(f"key = {fixture}") == []
    assert [f.detector for f in scanner.findings(f"key = {AWS}")] == ["AWSKeyDetector"]


def test_an_allowed_value_too_short_to_be_specific_is_refused():
    with pytest.raises(ValueError, match="allow"):
        SecretScanner(allow=["AKIA"])


def test_the_default_set_is_every_regex_detector_but_public_ips():
    assert "IPPublicDetector" in known_detectors()
    assert "IPPublicDetector" not in DEFAULT_DETECTORS
    assert set(DEFAULT_DETECTORS) == set(known_detectors()) - {"IPPublicDetector"}
    assert SecretScanner().findings("server at 8.8.8.8 port 53") == []


def test_a_narrowed_detector_list_ignores_the_others():
    scanner = SecretScanner(detectors=["PrivateKeyDetector"])
    assert scanner.findings(AWS) == []
    assert [f.kind for f in scanner.findings(PEM)] == ["Private Key"]


def test_an_unknown_detector_is_refused():
    with pytest.raises(ValueError, match="NoSuch"):
        SecretScanner(detectors=["NoSuch"])


# ── masking ───────────────────────────────────────────────────────────────────


def test_mask_covers_the_whole_token():
    # detect-secrets reports only the first group ("ghp"); the mask must cover it all
    masked, counts = SecretScanner().mask(f"token: {GITHUB} end")
    assert GITHUB[4:] not in masked and "ghp_" not in masked
    assert masked == "token: [MASKED SECRET: GitHub Token] end"
    assert counts == {"GitHubTokenDetector": 1}


def test_mask_covers_a_whole_pem_block():
    masked, _ = SecretScanner().mask(f"key file:\n{PEM}\nafter")
    for line in PEM_BODY.splitlines():
        assert line not in masked
    assert masked.startswith("key file:\n") and masked.endswith("\nafter")


def test_a_pem_header_without_its_end_masks_to_the_end():
    truncated = "-----" + "BEGIN OPENSSH PRIVATE KEY-----\n" + PEM_BODY
    masked, _ = SecretScanner().mask("before\n" + truncated)
    assert masked.startswith("before\n") and "b3BlbnNz" not in masked


def test_two_secrets_are_both_masked_and_counted():
    masked, counts = SecretScanner().mask(f"a={AWS}\nb={AWS}\nc={SLACK}")
    assert AWS not in masked and SLACK not in masked
    assert counts == {"AWSKeyDetector": 2, "SlackDetector": 1}


# ── policy: block or mask, and the event ──────────────────────────────────────


def test_block_raises_without_the_value():
    seen, block, _ = events()
    with pytest.raises(SecretBlocked) as raised:
        block.check_text(f"use {AWS}", "model-call")
    assert AWS not in str(raised.value)
    assert "AWS Access Key" in str(raised.value)
    assert seen == [SecretScanEvent("blocked", "model-call", {"AWSKeyDetector": 1})]
    assert raised.value.event == seen[0]


def test_mask_returns_the_masked_text_and_records_it():
    seen, _, mask = events()
    assert mask.check_text(f"use {AWS}", "tool:read_file") == (
        "use [MASKED SECRET: AWS Access Key]"
    )
    assert seen == [SecretScanEvent("masked", "tool:read_file", {"AWSKeyDetector": 1})]


def test_clean_text_is_returned_as_is_with_no_event():
    seen, block, mask = events()
    assert block.check_text(CODE, "x") == CODE and mask.check_text(CODE, "x") == CODE
    assert seen == []


def test_a_json_body_with_an_escaped_pem_is_masked_and_stays_json():
    body = json.dumps({"messages": [{"role": "user", "content": f"read:\n{PEM}"}], "n": 1})
    seen, _, mask = events()
    sent = mask.check_body(body.encode(), "model-call")
    decoded = json.loads(sent)
    assert "[MASKED SECRET: Private Key]" in decoded["messages"][0]["content"]
    assert "MIIEowIBAAKCAQEAx4f0Zq1" not in sent.decode()
    assert decoded["n"] == 1
    assert seen[0].findings == {"PrivateKeyDetector": 1}


def test_a_json_secret_spelled_with_escapes_is_still_found():
    escaped = json.dumps({"content": AWS}).replace("A", "\\u0041", 1).encode()
    with pytest.raises(SecretBlocked):
        SecretScanner().check_body(escaped, "model-call")


def test_a_clean_body_is_returned_byte_for_byte():
    body = b'{"model":  "m",\n "messages": []}'
    assert SecretScanner().check_body(body, "model-call") is body


def test_a_non_json_body_keeps_its_other_bytes():
    body = b"\xff\xfe binary " + AWS.encode() + b" \x00tail"
    masked = SecretScanner("mask").check_body(body, "model-call")
    assert masked.startswith(b"\xff\xfe binary ") and masked.endswith(b" \x00tail")
    assert AWS.encode() not in masked


def test_two_secret_keys_stay_two_entries_when_masked():  # review: keys collided
    other = "AKIA" + "B" * 16
    body = json.dumps({"meta": {AWS: 1, other: 2}}).encode()
    meta = json.loads(SecretScanner("mask").check_body(body, "model-call"))["meta"]
    assert sorted(meta.values()) == [1, 2]
    assert all(AWS not in key and other not in key for key in meta)


def test_a_lone_surrogate_does_not_break_masking():  # review: utf-8 re-encode raised
    body = json.dumps({"a": "\ud800 " + AWS}).encode()
    masked = SecretScanner("mask").check_body(body, "model-call")
    assert AWS.encode() not in masked
    assert json.loads(masked)["a"] == "\ud800 [MASKED SECRET: AWS Access Key]"


def test_one_deadline_covers_a_whole_body(monkeypatch):  # review: it was per string
    deadlines: list[float] = []
    real = SecretScanner.findings

    def spy(self, text, deadline=None):
        deadlines.append(deadline)
        return real(self, text, deadline)

    monkeypatch.setattr(SecretScanner, "findings", spy)
    SecretScanner().check_body(json.dumps({"a": "x", "b": ["y", "z"]}).encode(), "model-call")
    assert len(deadlines) == 5  # two keys and three strings
    assert len(set(deadlines)) == 1 and deadlines[0] is not None


def test_text_that_cannot_be_scanned_in_time_fails_closed(monkeypatch):
    def too_slow(self, text, deadline=None):
        raise TimeoutError

    monkeypatch.setattr(SecretScanner, "findings", too_slow)
    assert SecretScanner("mask").check_text("anything", "model-call") == (
        "[MASKED: not scanned in time]"
    )
    with pytest.raises(SecretBlocked, match="could not be scanned in time"):
        SecretScanner().check_text("anything", "model-call")


def test_a_large_body_scans_quickly():
    body = json.dumps({"content": CODE * 15_000}).encode()  # ~1 MB
    start = time.perf_counter()
    SecretScanner().check_body(body, "model-call")
    assert time.perf_counter() - start < 2.0


# ── the audit event ───────────────────────────────────────────────────────────

LEDGER_ID = re.compile(r"[A-Za-z0-9._:/+=-]{1,128}")  # what an audit sink stores (no text)


def test_without_a_sink_the_event_is_a_structured_audit_log_record(caplog):
    with caplog.at_level(logging.WARNING, logger="agentic.audit"):
        SecretScanner("mask").check_text(f"key {AWS}", "model-call")

    (record,) = [r for r in caplog.records if r.name == "agentic.audit"]
    assert record.audit == {
        "event": "secret_scan",
        "action": "masked",
        "where": "model-call",
        "findings": {"AWSKeyDetector": 1},
    }
    assert AWS not in record.getMessage()


def test_event_fields_are_ids_an_audit_ledger_accepts():
    event = SecretScanEvent("blocked", "tool:read_file", {"AWSKeyDetector": 2})
    fields = event.fields()
    assert LEDGER_ID.fullmatch(fields["action"]) and LEDGER_ID.fullmatch(fields["where"])
    assert all(LEDGER_ID.fullmatch(name) for name in fields["findings"])
