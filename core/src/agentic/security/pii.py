"""Personal data in text about to be published centrally (M2-49, spec §15).

The publish scan (`agentic.security.publish_scan`) runs this detector beside the secret
scanner over every run event before it reaches the control plane. The patterns are
**precision-biased**, following TokenLean's G29 regex tier (Apache-2.0, same owner): an
event value is an id, a path, a digest or a timestamp, and masking one that merely looks
like personal data would corrupt the audit record the scan protects. So:

  * a card number must pass Luhn AND start like a card network's numbers, at that
    network's length;
  * a US SSN is matched only in its separated form (`123-45-6789`), never as nine bare
    digits, and never with an area number that is not issued (000, 666, 9xx);
  * an IBAN must pass its mod-97 check; a UK National Insurance number must use a valid
    prefix and suffix letter;
  * a phone number is an international `+<country> ...` number of 8-15 digits, or a
    North American number with separators;
  * every pattern refuses to start or end inside a longer run of letters and digits, so
    a hex digest, a run id or a version never matches part-way.

Every quantifier is bounded and every pattern anchors its start, so the scan is linear in
the text. Findings carry the kind and the span only - never the matched text.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

ENTITIES = ("email", "phone", "credit-card", "us-ssn", "uk-nino", "iban")


@dataclass(frozen=True)
class PiiFinding:
    entity: str
    start: int
    end: int


_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}"
    r"(?:\.[A-Za-z0-9\-]{1,63}){0,8}\.[A-Za-z]{2,63}(?![A-Za-z0-9\-])"
)
_PHONE_INTERNATIONAL = re.compile(r"(?<![\w+])\+[1-9]\d{0,3}(?:[ .\-]?\(?\d{1,4}\)?){2,6}(?!\w)")
_PHONE_NORTH_AMERICA = re.compile(
    r"(?<![\w+])(?:\+?1[ .\-]?)?(?:\(\d{3}\)|\d{3})[ .\-]\d{3}[ .\-]\d{4}(?!\w)"
)
_CARD = re.compile(r"(?<![A-Za-z0-9])(?:\d[ \-]?){12,18}\d(?![A-Za-z0-9])")
_SSN = re.compile(r"(?<![\w\-])(?!000|666|9\d\d)\d{3}([ \-])(?!00)\d{2}\1(?!0000)\d{4}(?![\w\-])")
_NINO = re.compile(
    r"(?<![A-Za-z0-9])(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]"
    r" ?\d{2} ?\d{2} ?\d{2} ?[A-D](?![A-Za-z0-9])"
)
_IBAN = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?(?![A-Za-z0-9])"
)


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _phone_ok(text: str) -> bool:
    return 8 <= len(_digits(text)) <= 15


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, ch in enumerate(digits):
        value = ord(ch) - 48
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_network_ok(digits: str) -> bool:
    """Whether the number starts like a card network's numbers, at its length."""
    n = len(digits)
    two, three, four = int(digits[:2]), int(digits[:3]), int(digits[:4])
    if digits[0] == "4":  # Visa
        return n in (13, 16, 19)
    if 51 <= two <= 55 or 2221 <= four <= 2720:  # Mastercard
        return n == 16
    if two in (34, 37):  # American Express
        return n == 15
    if four == 6011 or two in (62, 65) or 644 <= three <= 649 or two == 35:
        return 16 <= n <= 19  # Discover, UnionPay, JCB
    if two in (36, 38) or 300 <= three <= 305:  # Diners Club
        return n in (14, 16, 19)
    return False


def _card_ok(text: str) -> bool:
    digits = _digits(text)
    return 13 <= len(digits) <= 19 and _card_network_ok(digits) and _luhn_ok(digits)


def _iban_ok(text: str) -> bool:
    compact = text.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    moved = compact[4:] + compact[:4]
    number = "".join(str(int(ch, 36)) for ch in moved)
    return int(number) % 97 == 1


_RULES: dict[str, tuple[tuple[re.Pattern, Callable[[str], bool] | None], ...]] = {
    "email": ((_EMAIL, None),),
    "phone": ((_PHONE_INTERNATIONAL, _phone_ok), (_PHONE_NORTH_AMERICA, None)),
    "credit-card": ((_CARD, _card_ok),),
    "us-ssn": ((_SSN, None),),
    "uk-nino": ((_NINO, None),),
    "iban": ((_IBAN, _iban_ok),),
}


class PiiDetector:
    def __init__(self, entities: Iterable[str] | None = None) -> None:
        names = list(ENTITIES if entities is None else entities)
        if unknown := sorted(set(names) - set(ENTITIES)):
            raise ValueError(f"unknown PII entities {unknown}; known: {list(ENTITIES)}")
        if not names:
            raise ValueError("a PII detector needs at least one entity")
        self.entities = tuple(dict.fromkeys(names))

    def findings(self, text: str) -> list[PiiFinding]:
        """Every personal-data span in `text`, in order; overlapping spans are merged
        into the earliest, longest one."""
        found: list[PiiFinding] = []
        for entity in self.entities:
            for pattern, check in _RULES[entity]:
                for match in pattern.finditer(text):
                    if check is None or check(match.group(0)):
                        found.append(PiiFinding(entity, match.start(), match.end()))
        merged: list[PiiFinding] = []
        for finding in sorted(found, key=lambda f: (f.start, -f.end)):
            if merged and finding.start < merged[-1].end:
                continue
            merged.append(finding)
        return merged


__all__ = ["ENTITIES", "PiiDetector", "PiiFinding"]
