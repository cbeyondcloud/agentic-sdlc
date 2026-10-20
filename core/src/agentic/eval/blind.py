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
                      counts as a pass. It is deliberately broader than the scrubber (no
                      right-hand boundary, joiners interchangeable), so whatever the
                      scrubber misses is caught here rather than slipping through.

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


_JOINER = r"[-_./]"  # what joins the parts of an id: model-7, model_7, model.7, vendor/m
_ALNUM = r"[^\W_]"  # a letter or digit (\w without the underscore, which is a joiner)


def _term_core(term: str) -> str:
    """The term with its joiners made interchangeable or absent (model-7 = model_7 =
    model7), so a respelled id is still the same id."""
    parts = [re.escape(p) for p in re.split(_JOINER, term) if p]
    return f"{_JOINER}?".join(parts)


def _scrub_pattern(term: str) -> re.Pattern[str]:
    """The whole joined token around the term: a dated, versioned or prefixed id
    (`model-7-20260101`, `x.model-7`, `model-7b`) is redacted entirely. Token parts are
    letters and digits only, so a long snake_case name cannot make the match backtrack."""
    return re.compile(
        rf"(?<!{_ALNUM})(?:{_ALNUM}+[-_.])*{_term_core(term)}{_ALNUM}*"
        rf"(?:[-_.]{_ALNUM}+)*(?!{_ALNUM})",
        re.IGNORECASE,
    )


def _leak_pattern(term: str) -> re.Pattern[str]:
    """The term anywhere it starts a token or follows a joiner - no right boundary, so a
    suffix never hides it. A false hit only voids a run, the safe direction."""
    return re.compile(rf"(?<![^\W_]){_term_core(term)}", re.IGNORECASE)


class Scrubber:
    def __init__(self, names: Iterable[str] = (), markers: Iterable[str] = ()) -> None:
        self._names = [_scrub_pattern(term) for term in _terms(names)]
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
        self._patterns = [(term.lower(), _leak_pattern(term)) for term in _terms(forbidden)]
        self.leaks: list[str] = []
        self.inputs_seen = 0

    def see(self, text: str) -> None:
        self.inputs_seen += 1
        for term, pattern in self._patterns:
            if term not in self.leaks and pattern.search(text):
                self.leaks.append(term)
