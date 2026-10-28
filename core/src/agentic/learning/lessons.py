"""Lessons and how alike two of them are (M3-13, spec §24 "Detect repeats").

A lesson is one line an agent could follow next time. The Learning Analyst writes it
(`agentic.learning.analyst`); without a model, `heuristic_lesson` takes the first
sentence of what the person said (or, for an edit without notes, the first line they
added), so the loop works offline and in standalone mode.

Similarity is deterministic and testable - no model is called (owner decision 73):

  * `tokens` - lower-case words and code identifiers (`with_retry`), stop words dropped,
    each word reduced by a small suffix stemmer (`wrapping` -> `wrap`, `calls` -> `call`);
  * `LexicalSimilarity` - the Dice coefficient of two token sets
    (2|A n B| / (|A| + |B|)), and 0 when they share fewer than two words;
  * `alike` - a score above the policy's `similarity` (default 0.5), so "Add tests" and
    "Add logging" (exactly 0.5) are not the same correction;
  * `EmbeddingSimilarity` - cosine similarity over vectors from an `Embedder` port, for an
    org that wires an embedding model; nothing in the framework wires one by default.
"""

import math
import re
from collections.abc import Sequence
from typing import Protocol

from agentic.learning.records import MAX_LESSON, FeedbackRecord, cap

_WORD = re.compile(r"[a-z0-9_]+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
STOP_WORDS = frozenset(
    """a an and are as at be but by do does for from has have here how i if in into is it its
    it's me my no not of on or our please so that the their them then there these this those
    to too us was we were what when where which while who why will with would you your can
    could should just also any all more some use using used""".split()
)
_SUFFIXES = ("ing", "ed", "es", "s")
#: How many content words two lessons must share to be alike at all.
MIN_SHARED = 2


def _one_line(text: str) -> str:
    return " ".join(text.split())


def first_sentence(text: str) -> str:
    line = _one_line(text)
    return _SENTENCE.split(line, maxsplit=1)[0] if line else ""


def heuristic_lesson(record: FeedbackRecord) -> str:
    """A one-line lesson without a model: the first sentence of the person's words, or
    the first line they added to a handoff."""
    text = first_sentence(record.text)
    if not text and record.diff:
        for line in record.diff.splitlines():
            if line.startswith("+") and not line.startswith("+++") and line[1:].strip():
                text = _one_line(line[1:])
                break
    return cap(text or f"{record.signal} on {record.kind or 'the work'}", MAX_LESSON)


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiouls":
        word = word[:-1]  # wrapp -> wrap
    return word


def tokens(text: str) -> frozenset[str]:
    """The lesson's content words, stemmed."""
    return frozenset(_stem(word) for word in _WORD.findall(text.lower()) if word not in STOP_WORDS)


class Similarity(Protocol):
    threshold: float

    def score(self, a: str, b: str) -> float: ...


class LexicalSimilarity:
    """Dice over stemmed content words: the default, and the only one wired by default."""

    def __init__(self, threshold: float = 0.5) -> None:
        self.threshold = threshold

    def score(self, a: str, b: str) -> float:
        """Dice, or 0 when the lessons share fewer than `MIN_SHARED` words (fewer only
        when one of them has fewer words): one generic word in common is no match."""
        left, right = tokens(a), tokens(b)
        shared = len(left & right)
        if not left or not right or shared < min(MIN_SHARED, len(left), len(right)):
            return 0.0
        return 2 * shared / (len(left) + len(right))


def alike(similarity: Similarity, a: str, b: str) -> bool:
    """Whether two lessons are the same correction: a score above the threshold (at it is
    not enough - two short lessons sharing one word of two score exactly 0.5)."""
    return similarity.score(a, b) > similarity.threshold


class Embedder(Protocol):
    """An embedding model behind a port (optional, off by default)."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbeddingSimilarity:
    """Cosine similarity of two lessons' embeddings; each text is embedded once."""

    def __init__(self, embedder: Embedder, threshold: float = 0.8) -> None:
        self.embedder, self.threshold = embedder, threshold
        self._cache: dict[str, list[float]] = {}

    def _vector(self, text: str) -> list[float]:
        if text not in self._cache:
            self._cache[text] = list(self.embedder.embed([text])[0])
        return self._cache[text]

    def score(self, a: str, b: str) -> float:
        left, right = self._vector(a), self._vector(b)
        norm = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(y * y for y in right))
        return sum(x * y for x, y in zip(left, right, strict=False)) / norm if norm else 0.0


__all__ = [
    "Embedder",
    "EmbeddingSimilarity",
    "LexicalSimilarity",
    "Similarity",
    "alike",
    "first_sentence",
    "heuristic_lesson",
    "tokens",
]
