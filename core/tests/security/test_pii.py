"""The PII detector behind the publish scan (M2-49): precision first - an id, a hash or a
timestamp in an event must never read as personal data, or masking would corrupt the
audit trail it protects."""

import time

import pytest

from agentic.security.pii import ENTITIES, PiiDetector

EMAIL = "jane.doe" + "@" + "example.com"
CARD = "4111 1111 " + "1111 1111"  # the documented Visa test number (Luhn-valid)
SSN = "123-45-" + "6789"
NINO = "AB 12 34 " + "56 C"
IBAN = "GB82 WEST " + "1234 5698 7654 32"  # the documented example (mod-97 valid)


def kinds(text: str, detector: PiiDetector | None = None) -> list[str]:
    return [f.entity for f in (detector or PiiDetector()).findings(text)]


@pytest.mark.parametrize(
    ("text", "entity"),
    [
        (f"mail {EMAIL} now", "email"),
        ("call +44 20 7946 0958 today", "phone"),
        ("call (415) 555-0132 today", "phone"),
        (f"card {CARD}.", "credit-card"),
        ("card 4111111111111111", "credit-card"),
        (f"ssn {SSN}", "us-ssn"),
        (f"nino {NINO}", "uk-nino"),
        (f"iban {IBAN}", "iban"),
    ],
)
def test_finds_each_entity(text, entity):
    found = PiiDetector().findings(text)
    assert [f.entity for f in found] == [entity]
    assert text[found[0].start : found[0].end].strip(" .") != ""


def test_spans_cover_the_value():
    text = f"x {EMAIL} y"
    (finding,) = PiiDetector().findings(text)
    assert text[finding.start : finding.end] == EMAIL


def test_card_needs_luhn_and_a_card_prefix():
    assert kinds("4111111111111112") == []  # Luhn fails
    assert kinds("1234567812345670") == []  # Luhn passes, no card network starts with 1


def test_bare_nine_digits_is_not_ssn():
    assert kinds("order 123456789") == []
    assert kinds("000-12-3456") == []  # an area number never issued


def test_invalid_iban_checksum_is_not_pii():
    assert kinds("GB83 WEST 1234 5698 7654 32") == []


@pytest.mark.parametrize(
    "text",
    [
        "feature-delivery-20261023-101500-ab12",
        "sha256:" + "4111111111111111" * 4,
        "a" + "4111111111111111" + "b",
        "2026-10-23T10:15:00+00:00",
        "git:" + "1234567890" * 4,
        "agentic@1.2.3",
        "handoffs/review-1-attempt2.md",
        "cost +0.05",
        "v10.20.30.40",
    ],
)
def test_ids_and_hashes_are_not_pii(text):
    assert kinds(text) == []


def test_entities_can_be_narrowed_and_unknown_ones_are_refused():
    assert kinds(f"{EMAIL} {SSN}", PiiDetector(["us-ssn"])) == ["us-ssn"]
    with pytest.raises(ValueError, match="unknown PII entities"):
        PiiDetector(["passport"])
    with pytest.raises(ValueError, match="at least one"):
        PiiDetector([])
    assert set(ENTITIES) >= {"email", "phone", "credit-card", "us-ssn", "uk-nino", "iban"}


def test_long_input_is_linear():
    started = time.monotonic()
    PiiDetector().findings(("a." * 50_000) + ("1 " * 50_000) + ("9" * 100_000))
    assert time.monotonic() - started < 5
