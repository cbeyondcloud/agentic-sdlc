"""The auditor's stdlib verifier hashes exactly as Zyroflo does (through sdlc_commons), so an
export it accepts is one the framework's own chain check accepts (M2-13, D34)."""

import random

from agentic.auditproof import entry_hash
from sdlc_commons.ledger import entry_hash as zyroflo_entry_hash


def _value(rng: random.Random, depth: int = 0):
    kind = rng.randrange(7 if depth < 3 else 4)
    if kind == 0:
        return rng.randrange(-(2**40), 2**40)
    if kind == 1:
        alphabet = "aZ09 -_\u00e9\u6f22" + chr(0x2028) + '"\\/\n'
        return "".join(rng.choice(alphabet) for _ in range(rng.randrange(12)))
    if kind == 2:
        return rng.choice([True, False, None])
    if kind == 3:
        return f"{rng.randrange(10**6)}.{rng.randrange(100):02d}"
    if kind == 4:
        return [_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {f"k{rng.randrange(20)}é": _value(rng, depth + 1) for _ in range(rng.randrange(4))}


def test_hash_matches_zyroflo():
    rng = random.Random(1234)
    for _ in range(300):
        entry = {f"f{n}": _value(rng) for n in range(rng.randrange(1, 8))}
        entry["hash"] = "sha256:" + "0" * 64
        if rng.random() < 0.3:
            entry["signature"] = {"keyid": "x"}
        assert entry_hash(entry) == zyroflo_entry_hash(entry)
