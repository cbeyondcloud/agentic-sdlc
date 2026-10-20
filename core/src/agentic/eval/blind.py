"""Blinding (M1-63, spike S10): nobody who scores an output may know which variant wrote it.

  * `Scrubber`      - before any judge or person sees an output, removes what gives the
                      variant away: the names that differ between variants (skills, agent
                      versions, models), "Using skill ..." and "As the ... agent" tells,
                      model self-identification, and markers a skill's eval metadata lists.
  * `assign_labels` - variants become X and Y, flipped per run from a recorded seed. The
                      mapping is sealed: it can be revealed only after the scores are in
                      (Zyroflo's `blind_and_score` order).
  * `LeakCheck`     - every judge input is searched for the forbidden terms. Any hit makes
                      the evaluation UNMEASURABLE: a leaked label voids the run, it never
                      counts as a pass.

Only variant-DISTINGUISHING terms are scrubbed and forbidden: the agent's own name is
the same for both variants (and "design" or "developer" are ordinary words), so it is
not one of them.
"""

import random
import re
from collections.abc import Iterable, Sequence

MIN_TERM = 3  # a shorter term ("qa", "x") would match ordinary text everywhere
REDACTED = "[redacted]"
_TELL_LINES = [
    # a skill announcing itself: "Using skill: x", "> Applying the skill x"
    re.compile(
        r"^[ \t>*-]*(?:using|applying|loaded|activating|activated) (?:the )?skills?\b.*(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    ),
    # model self-identification, in the first person only - "Label it as AI-generated" or
    # "the data model is built by the nightly job" in a document stays
    re.compile(
        r"^.*(?:\b(?:I am|I'm) (?:an? )?(?:AI|AI assistant|(?:large )?language model)\b"
        r"|\bAs an AI\b(?! *-)).*(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"^.*\b(?:I|I'm)(?: was| am)? (?:trained|developed|created|built|made) by\b.*(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    ),
]
# a role tell that opens a clause: "As the business analyst agent, I ..." (the comma or
# colon is required, so "As the delivery agent retries, ..." stays)
_ROLE_TELL = re.compile(r"\bAs (?:the|an?|your) [\w '-]{1,60}? agent[,:][ \t]*", re.IGNORECASE)


class BlindingError(ValueError):
    """Blinding was misused (a term too short to search for, a mapping revealed early)."""


def _terms(names: Iterable[str]) -> list[str]:
    terms = []
    for name in names:
        name = name.strip()
        if len(name) < MIN_TERM:
            raise BlindingError(
                f"a blinding term must be at least {MIN_TERM} characters, got {name!r}"
            )
        terms.append(name)
    return sorted(set(terms), key=lambda t: (-len(t), t))  # longest first


def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(term)}(?![\w-])", re.IGNORECASE)


class Scrubber:
    def __init__(self, names: Iterable[str] = (), markers: Iterable[str] = ()) -> None:
        self._names = [_term_pattern(term) for term in _terms(names)]
        self._markers = [m for m in markers if m]

    def scrub(self, text: str) -> str:
        for pattern in _TELL_LINES:
            text = pattern.sub("", text)
        text = _ROLE_TELL.sub("", text)
        for pattern in self._names:
            text = pattern.sub(REDACTED, text)
        for marker in self._markers:
            text = text.replace(marker, "")
        return text


class SealedMapping:
    """Which variant each opaque label stands for, revealed only after scoring."""

    def __init__(self, labels: dict[str, str], seed: int) -> None:
        self._labels = dict(labels)
        self._variants = {variant: label for label, variant in labels.items()}
        self.seed = seed
        self._scored = False

    @property
    def labels(self) -> list[str]:
        return sorted(self._labels)

    def label(self, variant: str) -> str:
        return self._variants[variant]

    def seal_scores(self) -> None:
        """Every score is in; from now on the mapping may be shown."""
        self._scored = True

    def reveal(self) -> dict[str, str]:
        if not self._scored:
            raise BlindingError("the label mapping is sealed until every output is scored")
        return dict(self._labels)


def assign_labels(variants: Sequence[str], seed: int) -> SealedMapping:
    """X (and Y) for one (or two) variants; which is which is a coin flip from `seed`."""
    if len(variants) not in (1, 2) or len(set(variants)) != len(variants):
        raise BlindingError("an evaluation compares one variant or two distinct variants")
    order = list(variants)
    if len(order) == 2 and random.Random(seed).random() < 0.5:
        order.reverse()
    return SealedMapping(dict(zip(("X", "Y"), order, strict=False)), seed)


class LeakCheck:
    """Searches every judge input for the forbidden terms; `leaks` lists those found."""

    def __init__(self, forbidden: Iterable[str] = ()) -> None:
        self._patterns = [(term.lower(), _term_pattern(term)) for term in _terms(forbidden)]
        self.leaks: list[str] = []
        self.inputs_seen = 0

    def see(self, text: str) -> None:
        self.inputs_seen += 1
        for term, pattern in self._patterns:
            if term not in self.leaks and pattern.search(text):
                self.leaks.append(term)
