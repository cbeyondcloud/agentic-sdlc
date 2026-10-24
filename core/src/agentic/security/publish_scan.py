"""Secret and personal-data scan before run events are published centrally (M2-49).

A run's events reach the control plane exactly as they are chained in the run ledger: the
control plane verifies every entry's hash, so a chained entry can never be edited on its
way out. The guard therefore works at two points:

  * `screen(event, fields)` - before an event is chained (`ForwardingRecorder.screen`).
    Under `mask` every secret (the M1-41 scanner's detectors and allowed values) and
    every personal-data span (`agentic.security.pii`) is replaced by `masked-<kind>`,
    an id-shaped marker the ledger accepts. Under `block` the fields are left as they
    are: the sink refuses them.
  * `check(run_id, entries)` - before the sink sends chained entries. An entry with a hit
    (written past the screen, by Zyroflo itself, or under `block`) cannot be masked any
    more, so `PublishBlocked` is raised: the clean entries before it may still be sent
    (the control plane needs the chain in order), but it and every later entry stay in
    the local run ledger only. A retry cannot change that, and the error says so.

Both audit what they did as a `publish_scan` event naming kinds and counts, never a value.
Fields naming the people the audit trail is about (`identity_fields`: an approver's
email) and the chain's own fields (seq, hash, run id, time, ext ids) are not scanned.
Text that cannot be scanned in time counts as a hit (fail closed).
"""

import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from agentic.security.pii import PiiDetector
from agentic.security.secret_scan import SecretScanner
from agentic.telemetry.sinks import SinkError

Action = Literal["block", "mask"]
#: The chain's own fields: ids, digests and times the ledger writes itself.
STRUCTURAL = frozenset({"schema_version", "run_id", "seq", "at", "event", "prev", "hash", "ext"})
#: What the run ledger takes beside an event's fields (`RunLedger.record`): ids only.
LEDGER_PARAMS = frozenset({"agent", "skills"})
DEFAULT_IDENTITY_FIELDS = ("approver", "owner", "actor")
UNSCANNABLE = "unscannable"

log = logging.getLogger("agentic.audit")


@dataclass(frozen=True)
class PublishScanEvent:
    """One scan that found something: the audit record. Every value is an id."""

    action: Literal["masked", "blocked"]
    where: str  # "event:step_finished", "sink:control-plane"
    findings: dict[str, int]  # detector, or "pii:<entity>" -> how many
    fields_hit: tuple[str, ...] = ()  # the event fields that held them
    run_id: str = ""
    seq: int = 0

    def fields(self) -> dict[str, object]:
        found: dict[str, object] = {
            "action": self.action,
            "where": self.where,
            "findings": dict(self.findings),
            "fields": list(self.fields_hit),
        }
        if self.run_id:
            found["run_id"] = self.run_id
        if self.seq:
            found["seq"] = self.seq
        return found


class PublishBlocked(SinkError):
    """A chained entry holds a secret or personal data: it and the entries after it are not
    sent (see the module). `index` is its place in the batch checked, so the clean entries
    before it can still go (`delivered` then says how many did)."""

    retryable = False  # a chained entry never changes: no retry sends it (DF-535)

    def __init__(
        self,
        run_id: str,
        seq: int,
        event: str,
        kinds: Sequence[str],
        fields: Sequence[str],
        action: str,
        *,
        index: int = 0,
    ) -> None:
        super().__init__(run_id, seq)
        self.run_id, self.seq, self.event = run_id, seq, event
        self.kinds, self.fields, self.action = tuple(kinds), tuple(fields), action
        self.index = index

    def __str__(self) -> str:
        if self.delivered:
            plural = "s before it were" if self.delivered != 1 else " before it was"
            sent = f"the {self.delivered} event{plural} sent"
        else:
            sent = "nothing was sent"
        return (
            f"run {self.run_id} event {self.seq} ({self.event}) holds a secret or personal "
            f"data ({', '.join(self.kinds)}) in {', '.join(self.fields)}. It is already "
            f"chained, so it cannot be masked, and a retry will not send it: {sent}, and "
            f"the run's events from {self.seq} on stay in the local run ledger only "
            f"(publish_scan action: {self.action})"
        )


def audit_log(event: PublishScanEvent) -> None:
    """The default `on_event`: a structured record on the `agentic.audit` logger."""
    audit = {"event": "publish_scan", **event.fields()}
    log.warning(
        "publish scan %s %s: %s",
        event.action,
        event.where,
        event.findings,
        extra={"audit": audit, "fields": audit},
    )


class PublishGuard:
    def __init__(
        self,
        scanner: SecretScanner,
        pii: PiiDetector,
        *,
        action: Action = "mask",
        identity_fields: Iterable[str] = DEFAULT_IDENTITY_FIELDS,
        on_event: Callable[[PublishScanEvent], None] | None = None,
    ) -> None:
        if action not in ("block", "mask"):
            raise ValueError(f"action is 'block' or 'mask', not {action!r}")
        self.scanner, self.pii, self.action = scanner, pii, action
        self.skip = STRUCTURAL | LEDGER_PARAMS | frozenset(identity_fields)
        self.on_event = on_event or audit_log

    @classmethod
    def from_policy(
        cls, policy, on_event: Callable[[PublishScanEvent], None] | None = None
    ) -> "PublishGuard":
        """From an `OrgPolicy`: `publish_scan`, with `secret_scan`'s detectors and allowed
        values (one definition of a secret for everything that leaves the machine)."""
        rules = policy.publish_scan
        scanner = SecretScanner(
            policy.secret_scan.action,
            policy.secret_scan.detectors,
            on_event=lambda event: None,  # this guard audits its own events
            allow=policy.secret_scan.allow_values,
        )
        return cls(
            scanner,
            PiiDetector(rules.pii),
            action=rules.action,
            identity_fields=rules.identity_fields,
            on_event=on_event,
        )

    # ── the two points ───────────────────────────────────────────────────────

    def screen(self, event: str, fields: dict) -> dict:
        """`fields` itself when clean or under `block`; else a masked copy, audited."""
        if self.action != "mask":
            return fields
        masked, counts, hit = self._walk_fields(fields, mask=True)
        if not counts:
            return fields
        self.on_event(PublishScanEvent("masked", f"event:{event}", _sorted(counts), hit))
        return masked

    def check(self, run_id: str, entries: Sequence[Mapping[str, object]]) -> None:
        """Raise `PublishBlocked` (audited) at the first entry holding a hit."""
        for index, entry in enumerate(entries):
            _, counts, hit = self._walk_fields(dict(entry), mask=False)
            if not counts:
                continue
            seq = entry.get("seq")
            seq = seq if isinstance(seq, int) else 0
            found = PublishScanEvent(
                "blocked", "sink", _sorted(counts), hit, run_id=run_id, seq=seq
            )
            self.on_event(found)
            kinds = sorted(_kind(name) for name in counts)
            event = str(entry.get("event"))
            raise PublishBlocked(run_id, seq, event, kinds, hit, self.action, index=index)

    def findings(self, fields: dict) -> dict[str, int]:
        """What a scan finds in `fields` (detector, or `pii:<entity>` -> how many), with
        nothing masked and nothing audited: for a caller that decides and audits itself
        (the personal backup holds a file back, M2-20)."""
        _, counts, _ = self._walk_fields(fields, mask=False)
        return _sorted(counts)

    # ── scanning ─────────────────────────────────────────────────────────────

    def _walk_fields(self, fields: dict, *, mask: bool) -> tuple[dict, Counter, tuple[str, ...]]:
        counts: Counter = Counter()
        hit: list[str] = []
        rebuilt = dict(fields)
        deadline = time.monotonic() + self.scanner.deadline_s
        for name, value in fields.items():
            if name in self.skip:
                continue
            found: Counter = Counter()
            new = _walk(value, lambda text, found=found: self._masked(text, found, deadline))
            if found:
                counts.update(found)
                hit.append(name)
                if mask:
                    rebuilt[name] = new
        return rebuilt, counts, tuple(sorted(hit))

    def _masked(self, text: str, counts: Counter, deadline: float) -> str:
        """`text` with every hit replaced by `masked-<kind>`; `counts` updated."""
        try:
            secrets = self.scanner.findings(text, deadline)
        except TimeoutError:
            counts[UNSCANNABLE] += 1
            return f"masked-{UNSCANNABLE}"
        spans = [(f.start, f.end, f.detector, "secret") for f in secrets]
        spans += [(f.start, f.end, f"pii:{f.entity}", f.entity) for f in self.pii.findings(text)]
        if not spans:
            return text
        parts, last = [], 0
        for start, end, counted, marker in sorted(spans, key=lambda s: (s[0], -s[1])):
            if start < last:
                continue  # inside a span already masked
            parts += [text[last:start], f"masked-{marker}"]
            counts[counted] += 1
            last = end
        parts.append(text[last:])
        return "".join(parts)


def _walk(value: object, fn: Callable[[str], str]) -> object:
    """`value` with `fn` applied to every string in it, keys included."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list | tuple):
        return [_walk(item, fn) for item in value]
    if isinstance(value, dict):
        rebuilt: dict = {}
        for key, item in value.items():
            new = fn(key) if isinstance(key, str) else key
            n = 1
            while new in rebuilt:  # two keys that mask alike stay two entries
                n += 1
                new = f"{fn(key)}-{n}"
            rebuilt[new] = _walk(item, fn)
        return rebuilt
    return value


def _sorted(counts: Counter) -> dict[str, int]:
    return dict(sorted(counts.items()))


def _kind(name: str) -> str:
    return name.removeprefix("pii:") if name.startswith("pii:") else f"secret {name}"


__all__ = ["PublishBlocked", "PublishGuard", "PublishScanEvent", "audit_log"]
