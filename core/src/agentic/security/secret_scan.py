"""Secret scan before content leaves the machine (M1-41, spec §15).

What a model is sent - a request body, a tool's output - is scanned first; a
secret blocks the call or is masked, as org policy says (`SecretScanPolicy`),
and every hit is reported as an audit event naming detector types and counts,
never a value.

The detectors are detect-secrets' (Yelp, Apache-2.0): its regex-based plugins'
`denylist` patterns. They are run here over whole strings rather than through
detect-secrets' `scan_line`, which reports only a pattern's first group (`ghp`
for a GitHub token, the header of a private key - too little to mask) and costs
about half a millisecond a line. A mask covers the whole match, runs on over
token characters (a pattern may stop early), and a private key's header masks
through its END line. The entropy and keyword plugins need line heuristics and
are not offered. Matching uses `regex` with a deadline; text that cannot be
scanned in time is treated as a secret (fail closed).
"""

import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cache
from typing import Literal

import regex
from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.plugins.base import RegexBasedDetector

Action = Literal["block", "mask"]
#: Not secrets: a public IP address is reported by detect-secrets but leaks nothing.
NOT_SECRETS = frozenset({"IPPublicDetector"})
UNSCANNABLE = "Unscannable"  # the finding for text the deadline stopped
_KEY_DETECTOR = "PrivateKeyDetector"
_KEY_END = re.compile(r"-----END[^\n]{0,64}?-----")
_TOKEN_TAIL = re.compile(r"[A-Za-z0-9_\-+/=.]*")
_TOKEN = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-+/=.")
_FLAGS = ((re.IGNORECASE, regex.IGNORECASE), (re.VERBOSE, regex.VERBOSE))
DEADLINE_S = 10.0  # per scanned text, across all detectors

log = logging.getLogger("agentic.audit")


@dataclass(frozen=True)
class _Detector:
    name: str  # the detect-secrets plugin class name, as policy names it
    kind: str  # what it finds, in words ("AWS Access Key")
    patterns: tuple[regex.Pattern, ...]


@cache
def _catalogue() -> dict[str, _Detector]:
    found: dict[str, _Detector] = {}
    for kind, plugin in get_mapping_from_secret_type_to_class().items():
        if not issubclass(plugin, RegexBasedDetector):
            continue
        patterns = tuple(
            regex.compile(p.pattern, sum(ours for theirs, ours in _FLAGS if p.flags & theirs))
            for p in plugin().denylist
        )
        found[plugin.__name__] = _Detector(plugin.__name__, kind, patterns)
    return dict(sorted(found.items()))


def known_detectors() -> list[str]:
    """Every detector a policy may name: detect-secrets' regex-based plugins."""
    return list(_catalogue())


DEFAULT_DETECTORS = tuple(name for name in _catalogue() if name not in NOT_SECRETS)


@dataclass(frozen=True)
class Finding:
    detector: str
    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class SecretScanEvent:
    """One scan that found something: the audit record. Every value is an id."""

    action: Literal["blocked", "masked"]
    where: str  # "model-call", "tool:read_file", ...
    findings: dict[str, int]  # detector -> how many

    def fields(self) -> dict[str, object]:
        return {"action": self.action, "where": self.where, "findings": dict(self.findings)}


class SecretBlocked(Exception):
    """Content held a secret and policy blocks it. Names kinds and counts, never values."""

    def __init__(self, event: SecretScanEvent, kinds: Iterable[str]) -> None:
        self.event, self.kinds = event, sorted(set(kinds))
        self.total = sum(event.findings.values())
        super().__init__(
            f"{event.where}: refused before sending - it holds {self.total} secret(s) "
            f"({', '.join(self.kinds)}) and the secret_scan policy is 'block'. "
            f"Remove the secret, or ask an admin for secret_scan action 'mask'."
        )


def audit_log(event: SecretScanEvent) -> None:
    """The default `on_event`: a structured record on the `agentic.audit` logger, its
    `audit` attribute the event (the run's audit sink, M1-44, takes it from there)."""
    audit = {"event": "secret_scan", **event.fields()}
    log.warning(
        "secret scan %s %s: %s", event.action, event.where, event.findings, extra={"audit": audit}
    )


class SecretScanner:
    def __init__(
        self,
        action: Action = "block",
        detectors: Iterable[str] | None = None,
        *,
        on_event: Callable[[SecretScanEvent], None] | None = None,
        deadline_s: float = DEADLINE_S,
    ) -> None:
        if action not in ("block", "mask"):
            raise ValueError(f"action is 'block' or 'mask', not {action!r}")
        names = list(DEFAULT_DETECTORS if detectors is None else detectors)
        catalogue = _catalogue()
        if unknown := sorted(set(names) - set(catalogue)):
            raise ValueError(f"unknown detectors {unknown}; known: {list(catalogue)}")
        if not names:
            raise ValueError("a scanner needs at least one detector")
        self.action, self.deadline_s = action, deadline_s
        self.detectors = [catalogue[name] for name in names]
        self.on_event = on_event or audit_log

    @classmethod
    def from_policy(
        cls, policy, on_event: Callable[[SecretScanEvent], None] | None = None
    ) -> "SecretScanner":
        """From `OrgPolicy.secret_scan` (a `SecretScanPolicy`)."""
        return cls(policy.action, policy.detectors, on_event=on_event)

    # ── finding and masking ───────────────────────────────────────────────────

    def findings(self, text: str) -> list[Finding]:
        """Every secret in `text`, as merged spans in order. Raises TimeoutError."""
        deadline = time.monotonic() + self.deadline_s
        spans: list[Finding] = []
        for detector in self.detectors:
            for pattern in detector.patterns:
                left = max(deadline - time.monotonic(), 0.001)
                for match in pattern.finditer(text, timeout=left):
                    if match.end() > match.start():
                        spans.append(_span(detector, text, match.start(), match.end()))
        return _merged(spans)

    def mask(self, text: str) -> tuple[str, Counter]:
        """`text` with each secret replaced by a marker, and how many of each detector."""
        try:
            found = self.findings(text)
        except TimeoutError:
            return "[MASKED: not scanned in time]", Counter({UNSCANNABLE: 1})
        if not found:
            return text, Counter()
        parts, last = [], 0
        for finding in found:
            parts += [text[last : finding.start], f"[MASKED SECRET: {finding.kind}]"]
            last = finding.end
        parts.append(text[last:])
        return "".join(parts), Counter(f.detector for f in found)

    # ── applying policy ───────────────────────────────────────────────────────

    def check_text(self, text: str, where: str) -> str:
        """`text` itself if clean; else the masked text, or SecretBlocked - with an event."""
        masked, counts = self.mask(text)
        return masked if self._decide(counts, where) else text

    def check_body(self, content: bytes, where: str) -> bytes:
        """A request body: `content` itself if clean, else masked or SecretBlocked.
        In JSON every string is scanned decoded (escapes cannot hide a secret) and the
        body re-encoded only when masked; any other body is scanned as latin-1, so
        the bytes around a mask are kept exactly."""
        try:
            data = json.loads(content) if content else None
        except (ValueError, UnicodeDecodeError):
            text = content.decode("latin-1")
            masked = self.check_text(text, where)
            return content if masked is text else masked.encode("latin-1")
        counts: Counter = Counter()

        def mask(value: str) -> str:
            masked, found = self.mask(value)
            counts.update(found)
            return masked

        rebuilt = _walk(data, mask)
        if not self._decide(counts, where):
            return content
        return json.dumps(rebuilt, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _decide(self, counts: Counter, where: str) -> bool:
        """True when masked text is to be used; raises when blocked; False when clean."""
        if not counts:
            return False
        action = "blocked" if self.action == "block" else "masked"
        event = SecretScanEvent(action, where, dict(sorted(counts.items())))
        self.on_event(event)
        if self.action == "block":
            raise SecretBlocked(event, (self._kind(name) for name in counts))
        return True

    def _kind(self, detector: str) -> str:
        found = _catalogue().get(detector)
        return found.kind if found else "content that could not be scanned in time"


def _span(detector: _Detector, text: str, start: int, end: int) -> Finding:
    if detector.name == _KEY_DETECTOR:
        while start > 0 and text[start - 1] == "-":
            start -= 1
        closing = _KEY_END.search(text, end)
        end = closing.end() if closing else len(text)
    elif text[end - 1] in _TOKEN:
        end = _TOKEN_TAIL.match(text, end).end()
    return Finding(detector.name, detector.kind, start, end)


def _merged(spans: list[Finding]) -> list[Finding]:
    merged: list[Finding] = []
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if merged and span.start < merged[-1].end:
            last = merged[-1]
            if span.end > last.end:
                merged[-1] = Finding(last.detector, last.kind, last.start, span.end)
            continue
        merged.append(span)
    return merged


def _walk(value: object, fn: Callable[[str], str]) -> object:
    """`value` with `fn` applied to every string in it, keys included."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_walk(item, fn) for item in value]
    if isinstance(value, dict):
        return {fn(key): _walk(item, fn) for key, item in value.items()}
    return value
